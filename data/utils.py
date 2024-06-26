import re
import random
import math
import copy
import logging
from typing import Callable, Union, Set, Optional, List, Dict, Any, Tuple, MutableMapping  # noqa: 401
import dataclasses
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
import torch
from torch.nn.utils.rnn import pad_sequence
import transformers
import nltk
from more_itertools import locate
from nltk.corpus import stopwords
import numpy as np

stop_words = set(stopwords.words('english'))

try:
    from tqdm import trange
except(Exception):
    # We do not require tqdm package to be present
    def trange(x, *args, **kwargs):
        return range(x)

logger = logging.getLogger('data')


@dataclass
class DialogDatasetItem:
    context: Union[List[str], str]
    belief: Union[Dict[str, Dict[str, str]], str] = None
    database: Union[List[Tuple[str, int]], List[Tuple[str, int, Any]], None, str] = None
    response: str = None
    positive: bool = True
    raw_belief: Any = None
    raw_response: str = None
    user_input: Union[List[str], str] = None
    keywords: Union[List[str], str] = None

    def __getattribute__(self, name):
        val = object.__getattribute__(self, name)
        if name == 'belief' and val is None and self.raw_belief is not None:
            val = format_belief(self.raw_belief)
            self.belief = val
        return val


@dataclass
class DataCollatorWithPadding:
    tokenizer: Union[transformers.PreTrainedTokenizer,
                     transformers.PreTrainedTokenizerFast]
    max_length: Optional[int] = None

    def __call__(self, features: List[Dict[str, Union[List[int], torch.Tensor]]]) -> Dict[str, torch.Tensor]:
        max_len = max([len(x['response_labels']) for x in features])
        batch = {
            'consistency_labels': torch.tensor([x['consistency_labels'] for x in features], dtype=torch.float32),
            'consistency_token_ids': torch.tensor([x['consistency_token_ids'] for x in features], dtype=torch.int64),
            'input_ids': pad_sequence([torch.tensor(x['input_ids'], dtype=torch.int64) for x in features],
                                      batch_first=True, padding_value=self.tokenizer.pad_token_id),
            'belief_labels': pad_sequence([torch.tensor(x['belief_labels'], dtype=torch.int64) for x in features],
                                          batch_first=True, padding_value=-100),
            'response_labels': pad_sequence([torch.tensor(x['response_labels'], dtype=torch.int64) for x in features],
                                            batch_first=True, padding_value=-100),
            'user_labels': pad_sequence([torch.tensor(x['user_labels'], dtype=torch.int64) for x in features]+ [torch.empty(max_len)],
                                            batch_first=True, padding_value=-100)[:-1, :max_len],
            'belief_end': torch.tensor([x['belief_end'] for x in features], dtype=torch.int32),
            'res_end': torch.tensor([x['res_end'] for x in features], dtype=torch.int32),
        }
        return batch


class TokenizerTransformation:
    def __init__(self, tokenizer: transformers.GPT2Tokenizer, max_context_length: int = 500, add_keyword=None, alpha_blending=0.05):
        self.bob, self.eob, self.eokb, self.eok = tokenizer.convert_tokens_to_ids(
            ['=>', '<|eob|>', '<|eokb|>', '<|eok|>'])
        self.eos = tokenizer.eos_token_id
        self.tokenizer = tokenizer
        self.max_context_length = max_context_length
        self.add_keyword = add_keyword
        self.alpha_blending = alpha_blending

    def get_tokens(self, data):
        history, belief, database, user, keywords = data.context, data.belief, data.database, data.user_input, data.keywords
        response, positive = data.response, data.positive


        # Add history
        history = self.tokenizer.encode(history)
        inp = history
        labels = [-100 for _ in history]
        context_end = len(labels)

        # Add context
        if belief is not None:
            belief = [self.bob] + self.tokenizer.encode(belief) + [self.eob]
            inp += belief
            labels += belief

        belief_end = len(labels)

        # Add database
        if database is not None:
            database = self.tokenizer.encode(database) + [self.eokb]
            inp += database
            labels += [-100 for _ in database]

        database_end = len(labels)
        
        labels_user = labels.copy()
        
        """ def find_intersection(user, response):
            tags = []

            user_tags = [tag for word, tag in user]
            response_tags = [tag for word, tag in response]

            i, j = len(user_tags), 0
            while i > 0:
                interval = i
                tag = user_tags[len(user_tags) - i]
                if tag in response_tags:
                    indices = locate(response_tags, lambda x: x==tag)
                    for index in indices:
                        j = 0
                        for k in range(1, interval):
                            if user_tags[i: i + k] == response_tags[index : index + k]:
                                
                                if len(tags) > k:
                                    tags = user_tags[i: i + k]
                                i -= 1
                            else:

                                break """

############################################

        def lcs(a, b):
            tbl = [[0 for _ in range(len(b) + 1)] for _ in range(len(a) + 1)]
            for i, x in enumerate(a):
                for j, y in enumerate(b):
                    tbl[i + 1][j + 1] = tbl[i][j] + 1 if x == y else max(
                        tbl[i + 1][j], tbl[i][j + 1])
            res = []
            i, j = len(a), len(b)
            while i and j:
                if tbl[i][j] == tbl[i - 1][j]:
                    i -= 1
                elif tbl[i][j] == tbl[i][j - 1]:
                    j -= 1
                else:
                    res.append(a[i - 1])
                    i -= 1
                    j -= 1
            return res[::-1]

###############################################

        keyword_end = None
        if self.add_keyword is not None or keywords is not None:
            if keywords is None:
                if self.add_keyword == "pos_tags-user_overlap" or self.add_keyword == "pos_tags-ground_truth":
                    response_ = re.sub("[[*[a-z]*\s*[a-z]*]*]","", response)
                    response_ = nltk.word_tokenize(response)
                    response_ = [w.lower() for w in response_ if w.isalpha()] # Remove Punctuations 
                    response_pos_tags = nltk.pos_tag(response_)

                    if self.add_keyword == "pos_tags-user_overlap":
                        user_ = nltk.word_tokenize(user)    
                        
                        user_ = [w.lower() for w in user_ if w.isalpha()] # Remove Punctuations 
                        
                        
                        user_pos_tags = nltk.pos_tag(user_)

                        keys = lcs([tag for w, tag in response_pos_tags], [tag for w, tag in user_pos_tags])
                        
                    elif self.add_keyword == "pos_tags-ground_truth":

                        keys = [tag for w, tag in response_pos_tags]

                if self.add_keyword == "lexicons-user_overlap" or self.add_keyword == "lexicons-ground_truth" or self.add_keyword == "lexicons-alpha_blending":
                    
                    if self.add_keyword == "lexicons-user_overlap":
                        keys = [user_inp for user_inp in set(user.split()) if user_inp in response.split()]
                    elif self.add_keyword == "lexicons-ground_truth":
                        response = re.sub("[[*[a-z]*\s*[a-z]*]*]","", response)
                        response_ = [w.lower() for w in nltk.word_tokenize(response) if w.isalpha()] 
                        keys = [w for w in response_ if not w.lower() in stop_words]
                    elif self.add_keyword == "lexicons-alpha_blending":
                        overlap = [user_inp for user_inp in set(user.split()) if user_inp in response.split()]
                        user_cleaned = [w.lower() for w in set(nltk.word_tokenize(user)) if w.isalpha()] 
                        
                        if np.random.uniform(0,1) <= self.alpha_blending:
                            mean = 0.24
                            sd = 0.16
                            prob = np.random.normal(mean, sd)
                            num_ind = max(math.floor((len(user_cleaned))*prob), 0)
                            randices = np.random.choice(np.arange(len(user_cleaned)), num_ind, replace = False)
                            keys = [user_cleaned[i] for i in randices]
                        else:
                            keys = overlap




                keywords = self.tokenizer.encode("Keywords: "+str(", ".join(keys)) + " <|eok|> ")# + [self.tokenizer.convert_tokens_to_ids("<|eok|>")]
                inp += keywords
                labels += [-100 for _ in keywords]
                keyword_end = len(labels)

            else:
                keywords = self.tokenizer.encode("Keywords: "+str(", ".join(keywords)) + " <|eok|> ")
                inp += keywords
                labels += [-100 for _ in keywords]
                keyword_end = len(labels)


        # Add response
        if response is not None:
            response = self.tokenizer.encode(str(response)) + [self.eos]
            inp += response
            labels += response
            labels_user += self.tokenizer.encode(re.sub("[,.!?]", "", user))

        if positive is not None and not positive:
            labels = [-100 for _ in labels]
            labels_user = [-100 for _ in labels_user]

        if self.max_context_length > 0:
            old_length = len(inp)
            inp = inp[-self.max_context_length:]
           
            labels = labels[-self.max_context_length:]
            labels_user = labels_user[-self.max_context_length:]
            
            belief_end = belief_end - (old_length - len(inp))
            context_end = context_end - (old_length - len(inp))
            database_end = database_end - (old_length - len(inp))
        
        print(inp)
        print(f"input: {self.tokenizer.batch_decode([inp])}\n")
        print(f"labels: {self.tokenizer.batch_decode([labels])}\n")

        return inp, labels, positive, belief_end, context_end, database_end, labels_user

    # -100 is mask token for LM
    # transforms into dict {"input_ids", "labels", "binary_labels", "binary_token_ids" }
    # binary_labels are used for task 3
    def __call__(self, data):
        inp, labels, positive, belief_end, context_end, database_end, labels_user = self.get_tokens(data)
        belief_labels = [x if i < belief_end else -100 for i, x in enumerate(labels)]
        response_labels = [x if i >= belief_end else -100 for i, x in enumerate(labels)]
        response_labels_user = [x if i >= belief_end else -100 for i, x in enumerate(labels_user)]
        return dict(input_ids=inp, belief_labels=belief_labels, response_labels=response_labels, user_labels=response_labels_user,
                    consistency_labels=positive, consistency_token_ids=len(labels) - 1, belief_end=belief_end, res_end=len(labels))


def default_translate_match(n):
    if n == 0:
        return 'no match'
    if n == 1:
        return '1 match'
    return f'{n} matches'


@dataclass
class InsertLabelsTransformation:
    user_label: str = 'User :'
    sys_label: str = 'System :'
    database_label: str = 'DB :'
    belief_label: str = 'Belief state :'

    def __call__(self, sample: DialogDatasetItem) -> DialogDatasetItem:
        if isinstance(sample, tuple):
            sample = DialogDatasetItem(*sample)
        # Transform context
        context = sample.context
        context = list(context)
        labels = self.user_label, self.sys_label
        for i in range(len(context) - 1, -1, -1):
            label, other = labels
            context[i] = label + ' ' + context[i]
            labels = other, label
        context = ' '.join(context)

        # Database
        database = sample.database
        if database is not None:
            database_str = []
            for database_domain, database_count in database.items():
                database_str.append(database_domain + ' ' +
                                    default_translate_match(database_count))
            database = self.database_label + ' ' + ' , '.join(database_str)

        # Belief state
        belief = sample.belief
        if belief is not None:
            belief = self.belief_label + ' ' + belief

        return dataclasses.replace(sample, belief=belief, database=database, context=context)


class BeliefParser:
    def __init__(self):
        self.slotval_re = re.compile(r"(\w[\w ]*\w) = ([\w\d: |']+)")
        self.domain_re = re.compile(r"(\w+) {\s*([\w,= :\d|']*)\s*}", re.IGNORECASE)

    def __call__(self, raw_belief: str):
        belief = OrderedDict()
        for match in self.domain_re.finditer(raw_belief):
            domain, domain_bs = match.group(1), match.group(2)
            belief[domain] = {}
            for slot_match in self.slotval_re.finditer(domain_bs):
                slot, val = slot_match.group(1), slot_match.group(2)
                belief[domain][slot] = val
        return belief


def format_belief(belief: OrderedDict) -> str:
    assert isinstance(belief, OrderedDict)
    str_bs = []
    for domain, domain_bs in belief.items():
        domain_bs = ', '.join([f'{slot} = {val}' for slot, val in sorted(domain_bs.items(), key=lambda x: x[0])])
        str_bs.extend([domain, '{' + domain_bs + '}'])
    return ' '.join(str_bs)


class FakeDatabase:
    def __init__(self, seed=None):
        self._rnd = random.Random(seed)

    def __call__(self, belief, return_results=False) \
            -> "Union[OrderedDict[str, Tuple[int, dict]], OrderedDict[str, int]]":
        results = OrderedDict()
        for key, bs in belief.items():
            count = random.randrange(-5, 15)
            items = [{} for i in range(count)]
            results[key] = (len(items), items) if return_results else len(items)
        return results


def merge_ontologies(ontologies):
    ontology = defaultdict(lambda: set())
    for o in ontologies:
        if o is None:
            continue
        for k, val in o.items():
            ontology[k].update(val)
    return ontology


@dataclass
class DialogDataset(torch.utils.data.Dataset):
    items: List[any]
    database: Any = None
    domains: List[str] = None
    lexicalizer: Any = None
    transform: Callable[[Any], Any] = None
    normalize_input: Callable[[str], str] = None
    ontology: Dict[Tuple[str, str], Set[str]] = None

    @staticmethod
    def build_dataset_without_database(items, *args, **kwargs):
        return DialogDataset(items, FakeDatabase(), *args, **kwargs)

    def __getitem__(self, index):
        item = self.items[index]
        if self.transform is not None:
            item = self.transform(item)
        return item

    def __len__(self):
        return len(self.items)

    def map(self, transformation):
        def trans(x):
            x = self.transform(x)
            x = transformation(x)
            return x
        return dataclasses.replace(self, transform=trans)

    def finish(self, progressbar: Union[str, bool] = False):
        if self.transform is None:
            return self

        ontology = defaultdict(lambda: set())
        domains = set(self.domains) if self.domains else set()

        items = []
        for i in trange(len(self),
                        desc=progressbar if isinstance(progressbar, str) else 'loading dataset',
                        disable=not progressbar):
            item = self[i]
            for k, bs in item.raw_belief.items():
                domains.add(k)
                for k2, val in bs.items():
                    ontology[(k, k2)].add(val)
            items.append(item)
        if self.ontology:
            ontology = merge_ontologies((self.ontology, ontology))
        return dataclasses.replace(self, items=items, transform=None, domains=domains, ontology=ontology)


def wrap_dataset_with_cache(dataset):
    dataset = copy.copy(dataset)
    old_get = dataset.__getitem__
    cache = dict()

    def cached_get(i):
        if i not in cache:
            cache[i] = old_get(i)
        return cache[i]
    dataset.__getitem__ = cached_get
    return dataset


class ConcatDialogDataset(torch.utils.data.ConcatDataset):
    def map(self, transformation):
        return ConcatDialogDataset([x.map(transformation) for x in self.datasets])

    def finish(self, progressbar: Union[str, bool] = False):
        dataset = DialogDataset(self, database=FakeDatabase(), transform=lambda x: x)
        return dataset.finish(progressbar)


class BlacklistItemsWrapper:
    def __init__(self, items, blacklist):
        self.items = items
        self._indexmap = []
        blacklist_pointer = 0
        for i in range(len(items)):
            if blacklist_pointer >= len(blacklist):
                self._indexmap.append(i)
            elif i < blacklist[blacklist_pointer]:
                self._indexmap.append(i)
            elif i == blacklist[blacklist_pointer]:
                blacklist_pointer += 1
        assert len(self._indexmap) == len(items) - len(blacklist)

    def __getitem__(self, idx):
        return self.items[self._indexmap[idx]]

    def __len__(self):
        return len(self._indexmap)


def wrap_dataset_with_blacklist(dataset, blacklist):
    return dataclasses.replace(dataset, items=BlacklistItemsWrapper(dataset.items, blacklist))


def split_name(dataset_name: str):
    split = dataset_name.rindex('-')
    return dataset_name[:split], dataset_name[split + 1:]


def sort_database(belief: OrderedDict, database: Dict[str, Dict[str, str]]) -> OrderedDict:
    database = {k: v for k, v in database.items()}
    first_db = None
    if belief:
        first_key = next(iter(belief.keys()))
        first_db = database.pop(first_key, None)
    items = [(first_key, first_db)] if first_db is not None else []
    items += [(k, v) for k, v in sorted(database.items(), key=lambda x: x[0])]
    return OrderedDict(items)


def sort_belief(belief: dict, active_domain: Optional[str]):
    belief = {k: OrderedDict(sorted(v.items(), key=lambda x: x[0])) for k, v in belief.items()}
    if active_domain is not None:
        active_domain = active_domain.lower()
        active_bs = belief.pop(active_domain, None)
    else:
        active_bs = None

    items = [(active_domain, active_bs)] if active_bs is not None else []
    items += [(k, v) for k, v in sorted(belief.items(), key=lambda x: x[0])]
    result = OrderedDict(items)
    return result
