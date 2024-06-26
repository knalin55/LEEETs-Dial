#!/bin/env python
import sys
import logging
import os
import argparse
import transformers
import torch
import datasets
from torch.nn.parallel import DistributedDataParallel
from torchvision.transforms import Compose as ComposeTransformation
import tensorboardX
from tqdm import tqdm
import wandb
from pipelines import AuGPTConversationalPipeline
from utils import Mean, LanguageAccuracy, BinaryAccuracy, DialogueAlignmentAccuracy
from utils import DistributedMetricsDict, setup_logging, pull_model, seed
import data
from model import AuGPTModel, add_custom_tokens, AuGPTConfig, AuGPTTokenizer
from generate import sample_to_conversation, conversation_to_sample, format_samples
from more_itertools import locate
import numpy as np

class TrainingPredictor:
    def __init__(self, pipeline, dataset, size=8, **kwargs):
        self.dataset = torch.utils.data.Subset(dataset, list(range(size)))
        self.pipeline = pipeline

    def __call__(self):
        add_labels = data.InsertLabelsTransformation()
        conversations = list(map(sample_to_conversation, self.dataset))
        conversations = self.pipeline(conversations)
        results = [x.generated_responses[-1] for x in conversations]
        results = format_samples(map(conversation_to_sample, conversations))
        labels = format_samples(self.dataset)
        contexts = [x.context for x in self.dataset]
        for i in range(len(contexts)):
            contexts[i] = add_labels((contexts[i], None, None, None, 1)).context
        return list(zip(contexts, labels, results))


class Trainer:
    def __init__(self, args, logger):
        self.args = args
        self.logger = logger

        self.config, self.tokenizer, self.model = None, None, None
        self.tb_writer = None
        self.train_dataloader, self.dev_dataloader = None, None
        self.dev_predictor = None
        self.optimizer, self.scheduler, self.global_step = None, None, 0
        self.epoch = None 
        self.wandb_runid = None
        self.instance_weights = args.instance_weights
        self.include_user_loss = args.include_user_loss
        self.sacrebleu = datasets.load_metric("sacrebleu", experiment_id=f"{args.response_loss}-{args.instance_weights}")
        self.dir_path = args.dir_path
        self.add_keyword = args.add_keyword
        self.rank_alpha_user = args.rank_alpha_user
        self.save_best = args.save_best
        self.include_unlikelihood = args.include_unlikelihood
        self.alpha_blending = args.alpha_blending
        self.score = 0.0


    def _initialize_logging(self):
        if self.is_master():
            # Initialize wandb and logging
            
            if self.dir_path is not None:
                wandb.init(dir=self.dir_path)
            else:
                wandb.init()

            wandb.config.update(self.args)
            self.tb_writer = tensorboardX.SummaryWriter(wandb.run.dir)

            if self.args.local_rank == 0:
                self.logger.info("Running distributed training with world size: %s", torch.distributed.get_world_size())
        self._synchronize_wandb_runid()

    def _synchronize_wandb_runid(self):
        if self.is_master():
            self.wandb_runid = f'{wandb.run.entity}/{wandb.run.project_name()}/{wandb.run.id}'

        # Synchronize wandb run id
        if self.args.local_rank != -1:
            if self.is_master():
                wandb_id_tensor = torch.tensor(list(map(ord, self.wandb_runid)), dtype=torch.uint8)
                wandb_id_tensor = torch.cat([wandb_id_tensor,
                                             torch.zeros(64 - len(self.wandb_runid), dtype=torch.uint8)])
                wandb_id_tensor = wandb_id_tensor.to(self.args.device)
            else:
                wandb_id_tensor = torch.zeros(64, dtype=torch.uint8).to(self.args.device)
            torch.distributed.broadcast(wandb_id_tensor, 0)
            self.wandb_runid = ''.join(chr(x) for x in wandb_id_tensor if x != 0)

    def _initialize_dataloaders(self) -> torch.utils.data.Dataset:
        transform = [data.InsertLabelsTransformation(),
                     data.TokenizerTransformation(self.tokenizer, add_keyword=self.add_keyword, alpha_blending=self.alpha_blending)]
        train_transform = list(transform)

        if self.args.backtranslations != 'none':
            self.logger.info('loading backtranslations augmentation')
            backtranslations_name = self.args.backtranslations if self.args.backtranslations != 'latest' \
                else self.args.train_dataset
            backtranslate_transformation = data.load_backtranslation_transformation(backtranslations_name)
            train_transform.insert(0, backtranslate_transformation)

        transform = ComposeTransformation(transform)
        train_transform = ComposeTransformation(train_transform)
        world_size = 1 if self.args.local_rank == -1 else torch.distributed.get_world_size()
        batch_size = self.args.batch_size // \
            (self.args.gradient_accumulation_steps * world_size)

        # Training dataloader
        use_blacklist = self.args.clean_samples
        dataset = data.load_dataset(self.args.train_dataset,
                                    restrict_domains=self.args.restrict_domains,
                                    is_master=self.is_master(), use_blacklist=use_blacklist) \
            .finish(progressbar='loading train dataset' if self.is_master() else False)
        if self.args.local_rank == -1:
            sampler = torch.utils.data.RandomSampler(dataset)
        else:
            sampler = torch.utils.data.DistributedSampler(dataset)

        train_dataset = data.NegativeSamplingDatasetWrapper(dataset, train_transform)
        sampler = data.NegativeSamplerWrapper(sampler)
        self.train_dataloader = torch.utils.data.DataLoader(
            train_dataset,
            sampler=sampler,
            collate_fn=data.DataCollatorWithPadding(self.tokenizer),
            pin_memory=True,
            batch_size=batch_size)

        # Dev dataloader
        dataset = data.load_dataset(self.args.dev_dataset,
                                    restrict_domains=self.args.restrict_domains,
                                    is_master=self.is_master(), use_blacklist=use_blacklist) \
            .finish(progressbar='loading dev dataset' if self.is_master() else False)
        self.prediction_pipeline = AuGPTConversationalPipeline(
            model=self.model.module if isinstance(self.model, DistributedDataParallel) else self.model,
            tokenizer=self.tokenizer,
            lexicalizer=dataset.lexicalizer,
            database=dataset.database,
            device=-1 if self.args.device.type == 'cpu' else torch.cuda.current_device())
        self.dev_predictor = TrainingPredictor(self.prediction_pipeline, dataset)
        if self.args.local_rank == -1:
            sampler = torch.utils.data.RandomSampler(dataset)
        else:
            sampler = transformers.trainer.SequentialDistributedSampler(dataset)
        dev_dataset = data.NegativeSamplingDatasetWrapper(dataset, transform)
        sampler = data.NegativeSamplerWrapper(sampler)
        self.dev_dataloader = torch.utils.data.DataLoader(
            dev_dataset,
            sampler=sampler,
            pin_memory=True,
            collate_fn=data.DataCollatorWithPadding(self.tokenizer),
            batch_size=self.args.batch_size // self.args.gradient_accumulation_steps)

        self.logger.info('datasets loaded, train size: {}, dev size: {}'.format(
                         len(train_dataset),
                         len(dev_dataset)))

    def is_master(self):
        return self.args.local_rank == -1 or torch.distributed.get_rank() == 0

    def _update_config_and_args(self, config, args):
        argdict = vars(args)
        for key, val in vars(config).items():
            if key in argdict and argdict[key] is not None:
                setattr(config, key, argdict[key])
            if key in argdict:
                setattr(args, key, getattr(config, key))
        if wandb.run:
            wandb.run.config.update(args, allow_val_change=True)

    def _load_model(self):
        # Load models
        model_name = pull_model(self.args.model)
        self.config = AuGPTConfig.from_pretrained(model_name)
        self._update_config_and_args(self.config, self.args)
        self.tokenizer = AuGPTTokenizer.from_pretrained(model_name)
        model = AuGPTModel.from_pretrained(model_name, config=self.config)
        if self.args.model == 'gpt2':
            self.tokenizer, model = add_custom_tokens(self.tokenizer, model)
        if self.args.fp16:
            self.scaler = torch.cuda.amp.GradScaler()
        self.tokenizer.pad_token = self.tokenizer.eos_token
        if self.is_master():
            wandb.watch(model, log_freq=max(1000, self.args.logging_steps))

        number_of_parameters = sum(x.numel() for x in model.parameters())
        self.logger.info(f'model loaded, number of parameters: {number_of_parameters}')
        self.model = model

    def _save(self, epoch=None):
        if not self.is_master():
            return
        if epoch is None:
            output_dir = wandb.run.dir
        else:
            output_dir = os.path.join(
                wandb.run.dir, '{}-{}'.format(self.epoch, self.global_step))
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)

        model = self.model
        if isinstance(model, DistributedDataParallel):
            model = model.module
        model.save_pretrained(output_dir)
        self.tokenizer.save_pretrained(output_dir)
        self.config.save_pretrained(output_dir)
        if self.prediction_pipeline.database is not None:
            self.prediction_pipeline.database.save(output_dir)
        if self.prediction_pipeline.lexicalizer is not None:
            self.prediction_pipeline.lexicalizer.save(output_dir)
        torch.save(self.args, os.path.join(output_dir, 'training_args.bin'))
        self.logger.info(f"saving model checkpoint to: {output_dir}")

    @torch.no_grad()
    def _run_validation(self):
        metrics = dict(loss=Mean(), lm_loss=Mean(), c_loss=Mean(),
                       bs_loss=Mean(), res_loss=Mean(), bs_acc=LanguageAccuracy(),
                       res_acc=LanguageAccuracy(), c_acc=BinaryAccuracy(), align_acc=DialogueAlignmentAccuracy())
        if self.args.local_rank != -1:
            metrics = DistributedMetricsDict(**metrics)
        for _, batch in enumerate(tqdm(self.dev_dataloader,
                                       desc='validation')):
            def _val_step(batch):
                batch = {k: v.to(self.args.device) for k, v in batch.items()}
                self.model.eval()

                def forward(batch):
                    output = self.model(**batch)
                    belief_loss, response_loss, response_ce, consistency_loss = output[:4]
                    loss = belief_loss + response_loss + consistency_loss
                    return loss, output[:6]

                if self.args.fp16:
                    with torch.cuda.amp.autocast():
                        loss, output = forward(batch)
                else:
                    loss, output = forward(batch)
                belief_loss, response_loss, response_ce, consistency_loss = output[:4]
                loss = belief_loss + response_loss + consistency_loss
                metrics['loss'](loss)
                metrics['lm_loss'](belief_loss + response_ce)
                metrics['bs_loss'](belief_loss)
                metrics['res_loss'](response_ce)
                metrics['c_loss'](consistency_loss)
                metrics['bs_acc'](output[4], batch['belief_labels'])
                metrics['res_acc'](output[4], batch['response_labels'])
                metrics['c_acc'](output[5], batch['consistency_labels'])
                metrics['align_acc'](output[4], batch['user_labels'])
            _val_step(batch)

        # Need to reduce here for other processes
        metric_values = {key: metric() for key, metric in metrics.items()}
        if self.tb_writer:

            # Write metrics to the tensorboard
            for k, value in metric_values.items():
                self.tb_writer.add_scalar('val_' + k, value, self.global_step)
            self.tb_writer.flush()
            wandb.log({'val_' + k: v for k, v in metric_values.items()}, step=self.global_step)
        return metric_values

    def _publish_artifact(self):
        artifact = wandb.Artifact(f'{wandb.run.name}-model', 'model')
        output_dir = wandb.run.dir
        for f in os.listdir(output_dir):
            if f.startswith('wandb-'):
                continue  # noqa: 701
            if f == 'output.log':
                continue  # noqa: 701
            if f == 'requirements.txt':
                continue  # noqa: 701
            if f.startswith('events.'):
                continue  # noqa: 701
            if os.path.isdir(f):
                continue  # noqa: 701
            artifact.add_file(os.path.join(output_dir, f), f)
        wandb.run.log_artifact(artifact)
        self.logger.info('model artifact published')

    @ torch.no_grad()
    def _run_prediction(self):
        self.model.eval()
        if self.tb_writer:
            # Predict some text
            # TODO: distributed inference
            sampled = self.dev_predictor()
            for i, (context, label, predicted) in enumerate(sampled):
                self.tb_writer.add_text(f'{i}.context', context, global_step=self.global_step)
                self.tb_writer.add_text(f'{i}.label', label, global_step=self.global_step)
                self.tb_writer.add_text(f'{i}.predicted', predicted, global_step=self.global_step)
            self.tb_writer.flush()

    def _log_oom(self, exc):
        msg = "OOM: Ran out of memory with exception: {}".format(exc)
        self.logger.warning(msg)
        if torch.cuda.is_available() and hasattr(torch.cuda, "memory_summary"):
            for device_idx in range(torch.cuda.device_count()):
                logger.warning(torch.cuda.memory_summary(device=device_idx))
        sys.stderr.flush()

    def _initialize_distributed_data_parallel(self, model):
        if self.args.local_rank != -1:
            model = DistributedDataParallel(
                model,
                device_ids=[self.args.local_rank],
                output_device=self.args.local_rank,
                find_unused_parameters=True)
        return model

    def train(self):
        self._initialize_logging()
        if self.args.device == torch.device('cpu'):
            self.logger.warning('running on CPU might have poor performance')
        if self.args.seed != -1:
            seed(self.args.seed)

        # Load model
        self._load_model()

        # Initialize data loaders
        self._initialize_dataloaders()

        # Finish model initialization
        self.model = self.model.to(self.args.device)
        self.model = self._initialize_distributed_data_parallel(self.model)

        # Initialize training
        t_total = int(len(self.train_dataloader) * self.args.epochs /
                      self.args.gradient_accumulation_steps)
        no_decay = ['bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in self.model.named_parameters() if not any(
                nd in n for nd in no_decay)], 'weight_decay': self.args.weight_decay},
            {'params': [p for n, p in self.model.named_parameters()
                        if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}]

        self.optimizer = transformers.AdamW(
            optimizer_grouped_parameters, lr=self.args.learning_rate, eps=self.args.adam_epsilon)
        self.scheduler = transformers.get_linear_schedule_with_warmup(
            self.optimizer,
            num_warmup_steps=self.args.warmup_steps,
            num_training_steps=t_total)
        self.optimizer.zero_grad()
        self.global_step = 0

        # Save initial model
        self._save()

        metrics = dict(loss=Mean(), lm_loss=Mean(), c_loss=Mean(),
                       bs_loss=Mean(), res_loss=Mean(), bs_acc=LanguageAccuracy(),
                       res_acc=LanguageAccuracy(), c_acc=BinaryAccuracy(), align_acc=DialogueAlignmentAccuracy())
        if self.args.local_rank != -1:
            metrics = DistributedMetricsDict(**metrics)
        if self.tb_writer:
            self.tb_writer.add_scalar('epoch', 0, self.global_step)
            wandb.log({'epoch': 0}, step=self.global_step)
        for epoch in range(self.args.epochs):
            self.epoch = epoch + 1
            self.train_dataloader.sampler.set_epoch(epoch)
            for i, batch in enumerate(tqdm(self.train_dataloader,
                                           desc=f'training epoch {epoch + 1}/{self.args.epochs}')):
                # We need to release memory here, therefore the closure
                def _train_step(batch):
                    self.model.train()
                    instance_weights = None
                    
                    batch = {k: v.to(self.args.device) for k, v in batch.items()}
                    def get_instance_weights(batch):
                        instance_weights = []
                        for input_ids in batch["input_ids"]:
                            input_text = self.tokenizer.decode(
                                input_ids).lower().split("user :")[-1] 
                            user_input, other = input_text.split("=>")
                            system_input = input_text.split('<|eokb|>')[-1].split(self.tokenizer.eos_token)[0]
                            results = self.sacrebleu.compute(predictions=[user_input.strip()], references=[[system_input]])
                            #instance_weights.append(10/(0.5 + np.exp(0.1373*(results["score"]-4))))
                            #instance_weights.append(10/(1 + np.exp(0.8*(-results["precisions"][0]+18.1473))) + 0.1)
                            if self.instance_weights == "simple":
                                if results["precisions"][0] <= 25:
                                    instance_weights.append(1.0)
                                else:
                                    instance_weights.append(10.0)

                            elif self.instance_weights == "mod_sigmoid":
                                instance_weights.append(10/(1 + np.exp(0.8*(-results["precisions"][0]+18.1473))) + 0.1)

                            else:
                                if results["precisions"][0] >= 50:
                                    instance_weights.append(0.5)
                                elif results["precisions"][0] >= 30 and results["precisions"][0] < 50:
                                    instance_weights.append(0.4)
                                elif results["precisions"][0] >= 20 and results["precisions"][0] < 30:
                                    instance_weights.append(0.3)
                                else:
                                    instance_weights.append(0.2)
                                

                        return torch.tensor(instance_weights).to(self.args.device)
                    
                    if self.instance_weights == "mod_sigmoid" or self.instance_weights == "simple": instance_weights = get_instance_weights(batch)

                    variable_key = None
                    if self.instance_weights == "variable_key": variable_key = get_instance_weights(batch)
                  
                    def forward(batch, user_input_labels=False, instance_weights=instance_weights, tokenizer=None, rank_alpha_user=None, do_weighted_useroverlap=False, include_unlikelihood=False):
                        output = self.model(
                            **batch, 
                            user_input_labels=user_input_labels, 
                            instance_weights=instance_weights, 
                            tokenizer=tokenizer, 
                            rank_alpha_user=rank_alpha_user, 
                            do_weighted_useroverlap=do_weighted_useroverlap,
                            include_unlikelihood= include_unlikelihood)
                            
                        belief_loss, response_loss, response_ce, consistency_loss = output[:4]
                        loss = belief_loss + response_loss + consistency_loss
                        return loss, output[:6]
                    

                    rank_alpha_user = variable_key if variable_key is not None else self.rank_alpha_user
                    do_weighted_useroverlap = True if variable_key is not None else False

                    if self.args.fp16:
                        with torch.cuda.amp.autocast():
                            loss, output = forward(
                                batch, 
                                user_input_labels=self.include_user_loss, 
                                rank_alpha_user=rank_alpha_user,
                                do_weighted_useroverlap = do_weighted_useroverlap)
                    else:
                        loss, output = forward(
                            batch, 
                            user_input_labels=self.include_user_loss, 
                            tokenizer=self.tokenizer, 
                            rank_alpha_user=rank_alpha_user,
                            do_weighted_useroverlap = do_weighted_useroverlap,
                            include_unlikelihood = self.include_unlikelihood)

                    belief_loss, response_loss, response_ce, consistency_loss = output[:4]
                    loss = belief_loss + response_loss + consistency_loss
                    metrics['loss'](loss)
                    metrics['lm_loss'](belief_loss + response_ce)
                    metrics['bs_loss'](belief_loss)
                    metrics['res_loss'](response_ce)
                    metrics['c_loss'](consistency_loss)
                    metrics['bs_acc'](output[4], batch['belief_labels'])
                    metrics['res_acc'](output[4], batch['response_labels'])
                    metrics['c_acc'](output[5], batch['consistency_labels'])
                    metrics['align_acc'](output[4], batch['user_labels'])
                    loss = loss / self.args.gradient_accumulation_steps
                    if self.args.fp16:
                        self.scaler.scale(loss).backward()
                    else:
                        loss.backward()

                _train_step(batch)
                del batch

                if (i + 1) % self.args.gradient_accumulation_steps == 0:
                    self.global_step += 1

                    # Use CUDA amp
                    if self.args.fp16:
                        self.scaler.unscale_(self.optimizer)
                        torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.args.max_norm)
                        self.scaler.step(self.optimizer)
                        self.scaler.update()
                    else:
                        torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.args.max_norm)
                        self.optimizer.step()

                    self.model.zero_grad()
                    self.scheduler.step()

                    # Logging
                    # Need to reduce here for other processes
                    metric_values = {key: metric() for key, metric in metrics.items()}
                    for _, metric in metrics.items():
                        metric.reset_states()

                    if self.global_step % self.args.logging_steps == 0 and self.tb_writer:
                        self.tb_writer.add_scalar('lr', self.scheduler.get_last_lr()[0], self.global_step)
                        wandb.log({'lr': self.scheduler.get_last_lr()[0]}, step=self.global_step)
                        for k, value in metric_values.items():
                            self.tb_writer.add_scalar(k, value, self.global_step)
                        self.tb_writer.flush()
                        wandb.log(metric_values, step=self.global_step)

                    # Validation
                    if self.global_step % self.args.validation_steps == 0:
                        self._run_validation()

            # Log learning rate for each epoch and save the checkpoint
            metric_values_val = self._run_validation()
            self._run_prediction()
            if self.tb_writer:
                self.tb_writer.add_scalar('lr', self.scheduler.get_last_lr()[0], self.global_step)
                self.tb_writer.add_scalar('epoch', epoch, self.global_step)
                self.tb_writer.flush()
                wandb.log({
                    'lr': self.scheduler.get_last_lr()[0],
                    'epoch': epoch
                }, step=self.global_step)
            
            if self.save_best:
                if (metric_values_val["align_acc"] + metric_values_val["res_acc"])/2 >= self.score:
                    self._save(epoch=epoch)
                    self.score = (metric_values_val["align_acc"] + metric_values_val["res_acc"])/2
            else:
                self._save(epoch=epoch)

        # Publish the model to wandb
        # self._publish_artifact()

        # Final evaluation
        if hasattr(self, 'run_evaluation'):
            self.run_evaluation()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='gpt2')
    parser.add_argument('--no-cuda', action='store_true')
    parser.add_argument('--train-dataset', default='multiwoz-2.1-train')
    parser.add_argument('--dev-dataset', default='multiwoz-2.1-val')
    parser.add_argument('--weight-decay', type=float, default=0.0)  # TODO
    parser.add_argument('--learning-rate', type=float, default=5e-5)  # this is soloist, I would try 6.25e-5
    parser.add_argument('--adam-epsilon', type=float, default=1e-8)
    parser.add_argument('--top-p', type=float, default=0.2)
    parser.add_argument('--num-beams', type=int, default=None)
    parser.add_argument('--max-norm', type=float, default=1.0)
    parser.add_argument('--warmup-steps', type=int, default=1000)
    parser.add_argument('--batch-size', type=int, default=4)
    parser.add_argument('--fp16', action='store_true')
    parser.add_argument('--logging-steps', type=int, default=200)
    parser.add_argument('--response-loss', choices=['unlikelihood', 'ce', 'user_overlap'], default=None)
    parser.add_argument('--evaluation-dialogs', type=int, default=1000)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--validation-steps', type=int, default=2000)
    parser.add_argument('--gradient-accumulation-steps', type=int, default=1)
    parser.add_argument('--clean-samples', action='store_true')
    parser.add_argument('--restrict-domains', action='store_true')
    parser.add_argument('--backtranslations', type=str, default='none')
    parser.add_argument('--epochs', default=10, type=int)
    parser.add_argument('--instance_weights', choices=['mod_sigmoid', 'simple', 'variable_key'], default=None)
    parser.add_argument('--include_user_loss', action='store_true')
    parser.add_argument('--dir_path', default=None, type=str)
    parser.add_argument('--add_keyword', choices=['pos_tags-user_overlap', 'pos_tags-ground_truth', 'lexicons-user_overlap', 'lexicons-ground_truth', 'lexicons-alpha_blending'], default=None)
    parser.add_argument('--rank_alpha_user', default=1.0, type=float)
    parser.add_argument('--alpha_blending', default=0.05, type=float)
    parser.add_argument('--save_best', action='store_true')
    parser.add_argument('--include_unlikelihood', action='store_true')

    # Passed by the launch script
    local_rank_default = int(os.environ['LOCAL_RANK']) if 'LOCAL_RANK' in os.environ else -1
    parser.add_argument('--local_rank', type=int, default=local_rank_default)

    args = parser.parse_args()
    use_cuda = torch.cuda.is_available() and not args.no_cuda \
        and torch.cuda.device_count() > 0
    args.device = torch.device('cuda' if use_cuda else 'cpu')
    assert args.batch_size % args.gradient_accumulation_steps == 0
    assert (args.batch_size // args.gradient_accumulation_steps) % 2 == 0, \
        "Negative samples must be balanced in the minibatch"
    return args


if __name__ == '__main__':
    args = parse_args()
    setup_logging()
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if args.local_rank != -1:
        torch.distributed.init_process_group('nccl', init_method='env://')
        assert args.device.type == 'cuda', "CUDA must be available in distributed training"
        torch.cuda.set_device(args.local_rank)
        logger.info('initialized distributed training with {} nodes, local-rank: {}'.format(
            torch.distributed.get_world_size(), args.local_rank))

    # Start training
    Trainer(args, logger).train()
    if args.local_rank != -1:
        torch.distributed.destroy_process_group()
