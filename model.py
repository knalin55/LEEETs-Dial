import dataclasses
import logging
from dataclasses import dataclass
from transformers import GPT2Tokenizer as AuGPTTokenizer  # noqa
from torch import nn
import transformers
from torch.nn import functional as F
import torch
import data
from nltk.tokenize import word_tokenize
from collections import defaultdict
from nltk.corpus import stopwords
import nltk
import math 
import evaluate

bleu = evaluate.load("sacrebleu")
stop_words = set(stopwords.words('english'))


EOB_TK = '<|eob|>'
EOKB_TK = '<|eokb|>'
EOT_TK = '<|endoftext|>'
SPECIAL_TOKENS = [EOB_TK, EOKB_TK]
logger = logging.getLogger()


def add_custom_tokens(tokenizer, model):
    tokenizer.add_special_tokens({'additional_special_tokens': SPECIAL_TOKENS})
    model.resize_token_embeddings(len(tokenizer))
    return tokenizer, model


# TODO: new transformers version
# @dataclass
# class AuGPTModelOutput(transformers.ModelOutput):
#     """
#     AuGPTModelOutput with consistency detection, split loss between belief state and response
#
#     Args:
#         loss (:obj:`torch.FloatTensor` of shape :obj:`(1,)`, `optional`, returned when ``labels`` is provided):
#             Language modeling loss.
#         mc_loss (:obj:`torch.FloatTensor` of shape :obj:`(1,)`, `optional`, returned when :obj:`mc_labels` is provided):
#             Multiple choice classification loss.
#         logits (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, num_choices, sequence_length, config.vocab_size)`):
#             Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
#         mc_logits (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, num_choices)`):
#             Prediction scores of the multiple choice classification head (scores for each choice before SoftMax).
#         past_key_values (:obj:`List[torch.FloatTensor]`, `optional`, returned when ``use_cache=True`` is passed or when ``config.use_cache=True``):
#             List of :obj:`torch.FloatTensor` of length :obj:`config.n_layers`,  with each tensor of shape
#             :obj:`(2, batch_size, num_heads, sequence_length, embed_size_per_head)`).
#
#             Contains pre-computed hidden-states (key and values in the attention blocks) that can be used (see
#             :obj:`past_key_values` input) to speed up sequential decoding.
#         hidden_states (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``output_hidden_states=True`` is passed or when ``config.output_hidden_states=True``):
#             Tuple of :obj:`torch.FloatTensor` (one for the output of the embeddings + one for the output of each layer)
#             of shape :obj:`(batch_size, sequence_length, hidden_size)`.
#
#             Hidden-states of the model at the output of each layer plus the initial embedding outputs.
#         attentions (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``output_attentions=True`` is passed or when ``config.output_attentions=True``):
#             Tuple of :obj:`torch.FloatTensor` (one for each layer) of shape
#             :obj:`(batch_size, num_heads, sequence_length, sequence_length)`.
#
#             Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
#             heads.
#     """
#
#     loss: Optional[torch.FloatTensor] = None
#     mc_loss: Optional[torch.FloatTensor] = None
#     logits: torch.FloatTensor = None
#     mc_logits: torch.FloatTensor = None
#     past_key_values: Optional[List[torch.FloatTensor]] = None
#     hidden_states: Optional[Tuple[torch.FloatTensor]] = None
#     attentions: Optional[Tuple[torch.FloatTensor]] = None


class AuGPTConfig(transformers.GPT2Config):
    def __init__(self,
                 summary_label_smoothing=0.1,
                 response_loss='unlikelihood',
                 **kwargs):
        super().__init__(**kwargs)
        self.summary_label_smoothing = summary_label_smoothing
        self.response_loss = response_loss

class UserRepeatCrossEntropyCriterion(nn.Module):
    def __init__(self, rank_alpha=1.0, ignore_index=-100, rank_alpha_neg=1.0, checkpoint=False, do_weighted_useroverlap=False):
        super().__init__()
        self.rank_alpha = rank_alpha
        self.ignore_index = ignore_index
        self.rank_alpha_neg = rank_alpha_neg
        self.do_weighted_useroverlap = do_weighted_useroverlap

    @torch.no_grad()
    def _negative_targets(self, lprobs, target):
        # E.g. DABCC | D | EFFGD => {A,B,C} are negative targets.
        # Make 'the triangle'.
        # TODO: cuda does not have short kernel for scatter, alternative?
        ntarget = target.add(1).masked_fill_(target == self.ignore_index, 0)
        ctx_cands = ntarget.unsqueeze(1).expand(ntarget.size(0), ntarget.size(1), ntarget.size(1))
        ctx_cands = ctx_cands.tril(-1)
        # Don't include the target for that timestep as a negative target.
        ctx_cands = ctx_cands.masked_fill(ctx_cands == ntarget.unsqueeze(2), 0)
        del ntarget
        

        negative_targets = lprobs.new_zeros(lprobs.shape[:2] + (lprobs.size(-1) + 1,))
        

        negative_targets = negative_targets.scatter_(2, ctx_cands, 1)


        return negative_targets[..., 1:]

    @torch.no_grad()
    def _positive_targets(self, lprobs, target, belief_end, res_end):
        # E.g. DABCC | D | EFFGD => {A,B,C} are negative targets.
        # Make 'the triangle'.
        # TODO: cuda does not have short kernel for scatter, alternative?
        ntarget = target.add(1).masked_fill_(target == self.ignore_index, 0)
        ctx_cands = ntarget.unsqueeze(1).expand(ntarget.size(0), ntarget.size(1), ntarget.size(1))
        #belief_end = (ctx_cands != 0).nonzero(as_tuple=False)
        ctx_cands_ = torch.zeros_like(ctx_cands)
        #ctx_cands_ = ctx_cands_.add(-100)
        #for i in range(ctx_cands_.shape[0]):
        #    i_ = [j for j in belief_end if j[0] == i]
        #    if len(i_) != 0: 
        #        ctx_cands_[i, i_[0][-1]:, :] = ctx_cands[i, i_[0][-1]:, :]
        for i, be in enumerate(belief_end):
            end_ = max([ctx_cands_.shape[1], res_end[i]])
            ctx_cands_[i, be:end_, :] = ctx_cands[i, be:end_, :]
        #ctx_cands = ctx_cands.tril(-1)
        # Don't include the target for that timestep as a negative target.
        #ctx_cands = ctx_cands.masked_fill(ctx_cands == ntarget.unsqueeze(2), 0)
        del ntarget
        

        positive_targets = lprobs.new_zeros(lprobs.shape[:2] + (lprobs.size(-1) + 1,))
        positive_targets = positive_targets.scatter_(2, ctx_cands_, 1)
        
        n_zero_tensor = torch.zeros_like(ctx_cands)

        positive_targets = positive_targets.scatter_(2, n_zero_tensor, 0)
        return positive_targets[..., 1:]

    def forward(self, logits, target_user, target_res, include_unlikelihood=False, return_ce=False, belief_end=None, res_end=None, instance_weights=None):
        """Loss which helps model not to predict already appeared tokens.
        Args:
            logits (tensor):
                Torch tensor of shape (bs, seq_len, vocab_size), output language
                model scores.
            target (tensor):
                Torch tensor of shape (bs, seq_len), language model target (model
                input tokens itself).
        Returns:
            Unlikelihood candidates loss-value.
        Notes:
            This loss is based on penalizing of the previous context tokens.
            Original paper - Welleck et al. https://arxiv.org/pdf/1908.04319.pdf.
        """
        lprobs = F.log_softmax(logits, -1)
        del logits
        positive_targets = self._positive_targets(lprobs, target_user, belief_end, res_end)
        # -- mle loss
        mle_loss = F.nll_loss(
            lprobs.view(-1, lprobs.size(-1)),
            target_res.view(-1),
            ignore_index=self.ignore_index,
            reduction='none',
        )

        mle_loss = mle_loss.sum() if instance_weights is None else (mle_loss*instance_weights).sum()
        
        # -- custom loss
        # Maximize (p(x_pt)) for positive user tokens x_pt (equivalently minimize -log(p(x_pt)))
        # - compute loss        
        _probs = torch.clamp(lprobs.exp(), min=1e-5)
        custom_loss = _probs * positive_targets
        cl = torch.sum(custom_loss, axis=-1)

        custom_loss_neg = None
        if include_unlikelihood:
            negative_targets = self._negative_targets(lprobs, target_res)
            one_minus_probs = torch.clamp((1.0 - lprobs.exp()), min=1e-5)
            custom_loss_neg = -torch.log(one_minus_probs) * negative_targets
            custom_loss_neg = custom_loss_neg.sum() if instance_weights is None else (torch.sum(custom_loss_neg, axis=-1).contiguous().view(-1)*instance_weights).sum()

        # -- custom loss
        # Maximize (1 - p(x_nt)) for negative target tokens x_nt (equivalently minimize -log(1-p(x_nt)))
        # - compute loss

        #custom_loss = custom_loss.sum()
        # Scale 
        if self.do_weighted_useroverlap:
            #cl_upd = torch.pow(cl, torch.unsqueeze(self.rank_alpha, -1).expand(cl.shape))
            cl[cl != 0] = -torch.log(cl[cl != 0])  
            custom_loss = cl*(torch.unsqueeze(self.rank_alpha, -1).expand(cl.shape))
            loss = mle_loss + custom_loss.sum()

        else:
            if instance_weights is None:
                custom_loss = -torch.log(cl[cl != 0]).sum()  
                loss = mle_loss + self.rank_alpha * custom_loss
            else:
                custom_loss = (torch.sum(-torch.log(cl[cl != 0]), axis=-1).contiguous().view(-1)*instance_weights).sum()
                loss = mle_loss + self.rank_alpha * custom_loss

        if custom_loss_neg is not None: loss += self.rank_alpha_neg * custom_loss_neg

        weight = (target_res != -100).sum()
        loss /= weight
        if return_ce:
            return loss, mle_loss / weight
        return loss



class CandidatePenaltyCrossEntropyCriterion(nn.Module):
    def __init__(self, rank_alpha=1.0, ignore_index=-100, checkpoint=False):
        super().__init__()
        self.rank_alpha = rank_alpha
        self.ignore_index = ignore_index

    @torch.no_grad()
    def _negative_targets(self, lprobs, target):
        # E.g. DABCC | D | EFFGD => {A,B,C} are negative targets.
        # Make 'the triangle'.
        # TODO: cuda does not have short kernel for scatter, alternative?
        ntarget = target.add(1).masked_fill_(target == self.ignore_index, 0)
        ctx_cands = ntarget.unsqueeze(1).expand(ntarget.size(0), ntarget.size(1), ntarget.size(1))
        ctx_cands = ctx_cands.tril(-1)
        # Don't include the target for that timestep as a negative target.
        ctx_cands = ctx_cands.masked_fill(ctx_cands == ntarget.unsqueeze(2), 0)
        del ntarget
        

        negative_targets = lprobs.new_zeros(lprobs.shape[:2] + (lprobs.size(-1) + 1,))
        

        negative_targets = negative_targets.scatter_(2, ctx_cands, 1)


        return negative_targets[..., 1:]

    def forward(self, logits, target, return_ce=False, instance_weights=None):
        """Loss which helps model not to predict already appeared tokens.
        Args:
            logits (tensor):
                Torch tensor of shape (bs, seq_len, vocab_size), output language
                model scores.
            target (tensor):
                Torch tensor of shape (bs, seq_len), language model target (model
                input tokens itself).
        Returns:
            Unlikelihood candidates loss-value.
        Notes:
            This loss is based on penalizing of the previous context tokens.
            Original paper - Welleck et al. https://arxiv.org/pdf/1908.04319.pdf.
        """
        lprobs = F.log_softmax(logits, -1)
        del logits
        negative_targets = self._negative_targets(lprobs, target)
        # -- mle loss
        mle_loss = F.nll_loss(
            lprobs.view(-1, lprobs.size(-1)),
            target.view(-1),
            ignore_index=self.ignore_index,
            reduction='none',
        )

        mle_loss = mle_loss.sum() if instance_weights is None else (mle_loss*instance_weights).sum()

        # -- custom loss
        # Maximize (1 - p(x_nt)) for negative target tokens x_nt (equivalently minimize -log(1-p(x_nt)))
        # - compute loss
        one_minus_probs = torch.clamp((1.0 - lprobs.exp()), min=1e-5)
        custom_loss = -torch.log(one_minus_probs) * negative_targets
        

        custom_loss = custom_loss.sum() if instance_weights is None else (torch.sum(custom_loss, axis=-1).contiguous().view(-1)*instance_weights).sum()

        #custom_loss = custom_loss.sum()
        # Scale loss
        loss = mle_loss + self.rank_alpha * custom_loss
        weight = (target != -100).sum()
        loss /= weight
        if return_ce:
            return loss, mle_loss / weight
        return loss


class LabelSmoothingCrossEntropyLoss(nn.Module):
    def __init__(self, smoothing=0.1):
        super().__init__()
        self.confidence = 1.0 - smoothing
        self.smoothing = smoothing

    def forward(self, pred, target, instance_weights=None):
        pred = pred.log_softmax(-1)
        with torch.no_grad():
            # true_dist = pred.data.clone()
            true_dist = torch.zeros_like(pred)
            true_dist.fill_(self.smoothing / (pred.size(-1) - 1))
            true_dist.scatter_(1, target.data.unsqueeze(1), self.confidence)
        loss = torch.squeeze(torch.sum(-true_dist * pred * (target != -100).unsqueeze(-1), dim=-1))*instance_weights
        loss = torch.sum(loss)
        return loss / (target != -100).sum()


class LabelSmoothingBCEWithLogitsLoss(nn.BCEWithLogitsLoss):
    def __init__(self, smoothing=0.1):
        super().__init__()
        self.smoothing = smoothing

    def forward(self, input, target, weight=None, instance_weights=None):
        smoothed_labels = target.mul(1 - 2 * self.smoothing).add_(self.smoothing)
        if instance_weights is None:
            return torch.nn.functional.binary_cross_entropy_with_logits(input, smoothed_labels, weight)
        else:
            
            loss = torch.nn.functional.binary_cross_entropy_with_logits(input, smoothed_labels, weight, reduction="none")
            weighted_loss = loss*instance_weights
            return torch.mean(weighted_loss)


class AuGPTModel(transformers.GPT2PreTrainedModel):
    authorized_missing_keys = [r"h\.\d+\.attn\.masked_bias",
                               r"lm\_head\.weight", r"binary\_head\.\w+"]

    def __init__(self, config):
        super().__init__(config)
        self.transformer = transformers.GPT2Model(config)
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        self.consistency_head = nn.Linear(config.n_embd, 1)
        self.auxiliary_dropout = nn.Dropout(config.summary_first_dropout)
        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head

    def forward(self,
                input_ids=None,
                past=None,
                attention_mask=None,
                token_type_ids=None,
                position_ids=None,
                head_mask=None,
                inputs_embeds=None,
                consistency_token_ids=None,
                consistency_labels=None,
                user_intent_token_ids=None,
                user_intent_labels=None,
                user_intent_mask=None,
                belief_labels=None,
                belief_end=None,
                res_end=None,
                system_action_token_ids=None,
                system_action_labels=None,
                system_action_mask=None,
                response_labels=None,
                user_labels=None,
                binary_labels=None,
                use_cache=None,
                output_attentions=None,
                output_hidden_states=None,
                instance_weights=None,
                user_input_labels=False, 
                rank_alpha_user=1.0,
                do_weighted_useroverlap=False,
                include_unlikelihood=False,
                **kwargs
                ):

        transformer_outputs = self.transformer(
            input_ids,
            past=past,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
        )

        hidden_states = transformer_outputs[0]
        lm_logits = self.lm_head(hidden_states)

        def gather_auxiliary_features(token_ids):
            if token_ids is None:
                token_ids = torch.full_like(hidden_states[..., :1, :],
                                            hidden_states.shape[-2]-1, dtype=torch.long,)
            else:
                token_ids = token_ids.unsqueeze(-1).unsqueeze(-1)
                token_ids = token_ids.expand(
                    (-1,) * (token_ids.dim() - 1) + (hidden_states.size(-1),))

            # shape of binary_token_ids: (bsz, XX, 1, hidden_size)
            # where XX are optional leading dim of hidden_states
            # shape of binary_logits (bsz, XX, hidden_size)
            logits = hidden_states.gather(-2, token_ids).squeeze(-2)
            logits = self.auxiliary_dropout(logits)
            return logits

        consistency_logits = self.consistency_head(gather_auxiliary_features(consistency_token_ids)).squeeze(-1)
        consistency_loss = None
        if consistency_labels is not None:
            # Auxiliary tasks
            aux_criterion = LabelSmoothingBCEWithLogitsLoss(self.config.summary_label_smoothing)
            consistency_loss = aux_criterion(consistency_logits, consistency_labels)

        
        def prepare_instance_wts(instance_weights, logits_size):
            if instance_weights is not None:
                instance_weights = torch.unsqueeze(instance_weights, -1).expand(-1, logits_size)
                instance_weights = torch.squeeze(instance_weights)
                return instance_weights.contiguous().view(-1)
            else:
                return None

        belief_loss, response_loss = None, None
        shift_user_labels = None

        if belief_labels is not None:
            assert response_labels is not None

            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_belief_labels = belief_labels[..., 1:].contiguous()
            shift_response_labels = response_labels[..., 1:].contiguous()
            
            if user_labels is not None: shift_user_labels = user_labels[..., 1:].contiguous()
                        
            loss_fct = nn.CrossEntropyLoss(reduction="none")
            
            belief_loss = loss_fct(
                shift_logits.view(-1, shift_logits.size(-1)),
                shift_belief_labels.view(-1))
            
            belief_loss = torch.mean(belief_loss) #if instance_weights is None else torch.mean(belief_loss*prepare_instance_wts(instance_weights, shift_logits.size(1)))

            if self.config.response_loss == 'ce':

                response_ce = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_response_labels.view(-1))
                response_loss = torch.mean(response_ce) if instance_weights is None else torch.mean(response_ce*prepare_instance_wts(instance_weights, shift_logits.size(1)))
            
            elif self.config.response_loss == 'unlikelihood':
                candidate_ce_fct = CandidatePenaltyCrossEntropyCriterion()
                response_loss, response_ce = candidate_ce_fct(
                    shift_logits,
                    shift_response_labels, 
                    return_ce=True, 
                    instance_weights=prepare_instance_wts(instance_weights, shift_logits.size(1)))
            
            elif self.config.response_loss == 'user_overlap':
                candidate_ce_fct = UserRepeatCrossEntropyCriterion(rank_alpha=rank_alpha_user, do_weighted_useroverlap=do_weighted_useroverlap)
                response_loss, response_ce = candidate_ce_fct(
                    shift_logits,
                    target_res = shift_response_labels, 
                    include_unlikelihood=include_unlikelihood, 
                    target_user = shift_user_labels, 
                    return_ce=True, 
                    belief_end=belief_end, 
                    res_end=res_end, 
                    instance_weights=prepare_instance_wts(instance_weights, shift_logits.size(1)))

            else:
                raise ValueError(f'Response loss {self.config.response_loss} is not supported')

        output = (lm_logits, consistency_logits,) + transformer_outputs[1:]
        if consistency_loss is not None:
            output = (consistency_loss,) + output
        return ((belief_loss, response_loss, response_ce) + output) if belief_loss is not None else output


@dataclass
class ModelPredictor:
    model: transformers.PreTrainedModel = None
    tokenizer: transformers.PreTrainedTokenizer = None
    max_belief_length: int = 100
    max_response_length: int = 200
    device: torch.device = torch.device('cpu')
    add_keyword: bool = False
    rerank: bool = False

    @staticmethod
    def from_pretrained(model_name):
        config = transformers.GPT2Config.from_pretrained(model_name)
        tokenizer = transformers.GPT2Tokenizer.from_pretrained(
            model_name)
        model = transformers.GPT2LMHeadModel.from_pretrained(model_name, config=config)
        if model_name == 'gpt2':
            tokenizer, model = add_custom_tokens(tokenizer, model)
        tokenizer.pad_token = tokenizer.eos_token
        predictor = ModelPredictor(model, tokenizer)
        return predictor

    def predict_belief(self, contexts):
        insert_labels = data.utils.InsertLabelsTransformation()
        tokenize = data.utils.TokenizerTransformation(
            self.tokenizer,
            max_context_length=self.model.config.n_ctx - self.max_belief_length - 1)
        eos_token_id = self.tokenizer.convert_tokens_to_ids(['<|eob|>'])[0]
        beliefs = []
        # TODO: batch generation
        for ctx in contexts:
            sample = insert_labels((ctx, None, None, None, 1))
            sample = tokenize.get_tokens(sample)[0]
            sample = torch.tensor(sample, dtype=torch.int64).to(self.device)
            sample = sample.view(1, *sample.shape)  # (batch, time)

            greedy_output = self.model.generate(
                input_ids=sample,
                max_length=sample.size(1) + self.max_belief_length,
                eos_token_id=eos_token_id,
                pad_token_id=eos_token_id,
                do_sample=False)
            # https://github.com/huggingface/transformers/blob/master/examples/text-generation/run_generation.py

            prediction = greedy_output[0]
            offset = len(sample[0])
            prediction = prediction[:offset + (prediction[offset:] != eos_token_id).int().sum()]
            prediction = self.tokenizer.decode(prediction, skip_special_tokens=False,
                                               clean_up_tokenization_spaces=True)
            prefix = self.tokenizer.decode(sample[0], clean_up_tokenization_spaces=True) +\
                '=> ' + insert_labels.belief_label
            prediction = prediction[len(prefix):]
            beliefs.append(prediction)
        return beliefs

    def predict_response(self, contexts, beliefs, dbs):
        insert_labels = data.utils.InsertLabelsTransformation()
        tokenize = data.utils.TokenizerTransformation(
            self.tokenizer,
            max_context_length=self.model.config.n_ctx - self.max_response_length)
        eos_token_id = self.tokenizer.convert_tokens_to_ids(['<|endoftext|>'])[0]
        responses = []

        def make_keywords(context, belief, db, model_kw, tokenizer_kw, user, threshold, stp_words=True, layer=-1):
            input_kw = f'{context} {belief} <|eob|> {db} <|eokb|> Keywords: '
            input_kw = tokenizer_kw.encode(input_kw, return_tensors='pt')[0][-self.model.config.n_ctx + self.max_response_length:]
            outputs = model_kw.to(device=self.device)(input_kw.to(device=self.device), output_attentions=True)  # Run model
            attention = outputs[-1]  # Retrieve attention from model outputs
            tokens = tokenizer_kw.convert_ids_to_tokens(input_kw)  # Convert input ids to token strings

            user_tok = tokenizer_kw.encode(" " + user)
            for i in range(input_kw.shape[-1]):
                if input_kw[i: i+len(user_tok)].tolist() == user_tok:
                    break
            
            if stp_words:
                word_tokens = word_tokenize(user)
                filtered_sentence = [w for w in word_tokens if not w.lower() in stop_words]
            else:
                user = user.translate(str.maketrans('', '', string.punctuation))
                filtered_sentence = [w for w in user.split()]

            attn_matrix = torch.mean(attention[layer], axis=1).fill_diagonal_(0)
            attn_matrix = attn_matrix.sum(axis=0)
            t = torch.argsort(attn_matrix, descending=True)
            #print([tokens[i] for i in t if i<=13 and i >= 2])
            #print([attn_matrix[i] for i in t if i<=13 and i >= 2])

            kw_score = defaultdict()

            for word in filtered_sentence:

                tokens_ = tokenizer_kw.convert_ids_to_tokens(tokenizer_kw.encode(" "+word))
                
                for j in range(i, len(tokens)):
                    if tokens[j:j+len(tokens_)] == tokens_:
                        kw_score[word] = max(attn_matrix[j:j+len(tokens_)]).cpu().detach().numpy()
                        i = j+len(tokens_)
                        break
            if len(kw_score.values()) != 0:
                max_score = max(kw_score.values())
                keywords_ = [key for key in kw_score.keys() if kw_score[key] >= max_score/threshold]
            else:
                keywords_ = filtered_sentence
                
            return keywords_

        # TODO: batch generation
        for context, belief, db in zip(contexts, beliefs, dbs):
            user = context[-1]
            sample = insert_labels((context, belief, db, None))
            
            keys = None 

            if self.add_keyword == "lexicons":
                
                keys = make_keywords(
                    sample.context, 
                    sample.belief, 
                    sample.database, 
                    self.model, 
                    self.tokenizer, 
                    user,
                    threshold=10, 
                    )
                
                sample.keywords = keys

            elif self.add_keyword == "pos_tags":        
                word_tokens = word_tokenize(user)
                filtered_sentence = [w for w in word_tokens if not w.lower() in stop_words]
                pos_tags = nltk.pos_tag(filtered_sentence)
                keys = [tag for w, tag in pos_tags]
                sample.keywords = keys

            sample = tokenize.get_tokens(sample)[0]
            sample = torch.tensor(sample, dtype=torch.int64).to(self.device)
            sample = sample.view(1, *sample.shape)  # (batch, time)

            greedy_output = self.model.generate(
                input_ids=sample,
                max_length=sample.size(1) + self.max_response_length,
                eos_token_id=eos_token_id,
                pad_token_id=eos_token_id,
                do_sample=True,
                top_k=0)
            # https://github.com/huggingface/transformers/blob/master/examples/text-generation/run_generation.py
            prediction = greedy_output[0]
        
            offset = len(sample[0])
            prediction = prediction[:offset + (prediction[offset:] != eos_token_id).int().sum()]
            prediction = self.tokenizer.decode(prediction, skip_special_tokens=False,
                                            clean_up_tokenization_spaces=True)
            prediction = prediction[len(self.tokenizer.decode(sample[0], clean_up_tokenization_spaces=True)):]
            prediction = prediction.lstrip()
            responses.append([prediction, keys])
        
        return responses

    def to(self, device):
        return dataclasses.replace(self, device=device, model=self.model.to(device))
