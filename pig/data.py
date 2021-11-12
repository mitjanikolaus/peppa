import torch
import torch.utils
from torch.utils.data import Dataset, IterableDataset, DataLoader
from torchvision.transforms import Normalize, Compose
import pig.transforms 
from dataclasses import dataclass
import glob
import os.path
import pig.preprocess 
import moviepy.editor as m
import pytorch_lightning as pl
import logging
from itertools import groupby
import pig.util
import torch.nn.functional as F
import json
import random
from typing import Union
import os.path
import math

@dataclass
class Clip:
    """Video clip with associated audio."""
    video: torch.tensor
    audio: torch.tensor
    duration: float
    filename: str
    offset: Union[float, None] = None
    index: Union[int, None] = None
    
@dataclass
class Pair:
    """Positive video-audio example."""
    video: torch.tensor
    audio: torch.tensor
    video_idx: int
    audio_idx: int


@dataclass
class RawPair:
    """Positive raw video-audio example."""
    video: m.VideoFileClip
    audio: m.AudioFileClip
    video_idx: int
    audio_idx: int

    
@dataclass
class ClipBatch:
    """Batch of video clips with associated audio."""
    video: torch.tensor
    audio: torch.tensor
    
    
def crop_audio_batch(audio):
    size = min(x.shape[1] for x in audio)
    return torch.stack([ x[:, :size] for x in audio ])

def pad_audio_batch(audio):
    size = max(x.shape[1] for x in audio)
    return torch.stack([ F.pad(x, (0, size-x.shape[1]), 'constant', 0) for x in audio ])

def crop_video_batch(video):
    size = min(x.shape[1] for x in video)
    return torch.stack([ x[:, :size, :, :] for x in video ])

def pad_video_batch(video):
    size = max(x.shape[1] for x in video)
    return torch.stack([ F.pad(x, (0,0, 0,0, 0,size-x.shape[1]), 'constant', 0) for x in video ])

                       
def collate(data):
    video, audio = zip(*[(x.video, x.audio) for x in data])
    return ClipBatch(video=pad_video_batch(video), audio=pad_audio_batch(audio))

class PeppaTripletDataset(Dataset):
    def __init__(self, raw=False):
        self.raw = raw

    def _cache_clips(self):
        try:
            self.dataset.clip_info = json.load(open(f"{self.dataset.clip_dir()}/clip_info.json"))
        except FileNotFoundError:
            self.dataset._cache_clips()
            
    def save_sample(self, sample):
        self.triplets = sample
        json.dump(self._triplets,
                  open(f"{self.dataset.clip_dir()}/triplet_info.json", "w"), indent=2)

    def resample(self):
        for info in _triplets(self.dataset.clip_info.values(), lambda x: x['duration']):
            yield info

    def _get_raw_item(self, idx):
        target_info, distractor_info = self.triplets[idx]
        with m.VideoFileClip(target_info['path']) as target:
            with m.VideoFileClip(distractor_info['path']) as distractor:
                return Triplet(anchor=target.audio, positive=target, negative=distractor)
                
    def __getitem__(self, idx):
        if self.raw:
            return self._get_raw_item(idx)
        else:
            triplet = self._get_raw_item(idx)
            positive = featurize_clip(triplet.positive)
            negative = featurize_clip(triplet.negative)
            return Triplet(anchor=positive.audio, positive=positive.video, negative=negative.video)
            
    def __len__(self):
        return len(self.triplets)

    
    @classmethod
    def from_dataset(cls, dataset):
        self.dataset = dataset
        self.save_sample()

    @classmethod
    def from_directory(cls, directory):
        raise NotImplementedError
    
class PeppaPigDataset(Dataset):
    def __init__(self, cache=True, cache_dir=None, **kwargs):
        dataset = PeppaPigIterableDataset(**kwargs)
        if cache_dir is None:
            self.cache_dir = f"data/out/items-{config_id(kwargs)}/"
        else:
            self.cache_dir = cache_dir
        if cache:
            os.makedirs(self.cache_dir, exist_ok=True)
            json.dump(kwargs, open(f"{self.cache_dir}/settings.json", "w"))
            for i, item in enumerate(dataset):
                logging.info(f"Caching item {i}")
                torch.save(item, f"{self.cache_dir}/{i}.pt")
        if not os.path.isdir(self.cache_dir):
            raise FileNotFoundError(f"No such directory: {self.cache_dir}")
        self.length = len(glob.glob(f"{self.cache_dir}/*.pt"))
        
    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        if idx >= self.length:
            raise IndexError("Index out of range")
        else:
            return torch.load(f"{self.cache_dir}/{idx}.pt")

    @classmethod
    def from_directory(cls, directory):
        return PeppaPigDataset(cache=False, cache_dir=directory)
    
class PeppaPigIterableDataset(IterableDataset):
    def __init__(self,
                 split=['val'],
                 target_size=(180, 100),
                 transform=None,
                 fragment_type='dialog',
                 duration=3.2,
                 jitter=False,
                 ):
        if type(split) is str:
            raise ValueError("`split` should be a list of strings")
        self.split = split
        self.target_size = target_size
        self.fragment_type = fragment_type
        self.duration = duration
        self.jitter = jitter
        self.settings = {**self.__dict__}
        self.transform = pig.util.identity if transform is None else transform 
        self.split_spec = dict(dialog=dict(train = range(1, 197),
                                           val  = range(197, 203),
                                           test = range(203, 210)),
                               narration=dict(val=range(1, 105),
                                              test=range(105, 210)))

    def featurize_clip(self, clip):
        frames = [ torch.tensor(frame/255).float()
                   for frame in clip.iter_frames() ]
        if len(frames) > 0:
            v = torch.stack(frames)
            a = torch.tensor(clip.audio.to_soundarray()).float()
            return Clip(video = self.transform(v.permute(3, 0, 1, 2)),
                        audio = a.mean(dim=1, keepdim=True).permute(1,0),
                        duration = clip.duration,
                        filename = clip.filename,
                        offset = clip.offset)
        else:
            raise ValueError("Clip has zero frames.")
        
    def _clips(self):
        for clip in self._raw_clips():
            try:
                yield self._featurize_clip(clip)
            except ValueError(e):
                pass

    def clip_dir(self):
        if self.permapath is None:
            return f"data/out/clips-{config_id(self.settings)}/"
        else:
            return self.permapath
    
    def _cache_clips(self):
        width,  height = self.target_size
        self.clip_info = {}
        os.makedirs(self.clip_dir(), exist_ok=True)
        json.dump(self.settings, open(f"{self.clip_dir()}/settings.json", "w"), indent=2)
        for i, clip in enumerate(self._raw_clips()):
            if clip.duration > 0:
                self.clip_info[i] = dict(path=f"{self.clip_dir()}/{i}.mp4",
                                         filename=clip.filename,
                                         offset=clip.offset,
                                         duration=clip.duration)
                #logging.info(f"Clip {i}: {clip.duration}s")
                clip.write_videofile(f"{self.clip_dir()}/{i}.mp4")
        json.dump(self.clip_info, open(f"{self.clip_dir()}/clip_info.json", "w"), indent=2)
        
        
    def _raw_clips(self):
        width,  height = self.target_size
        paths = [ path for split in self.split \
                       for episode_id in self.split_spec[self.fragment_type][split] \
                       for path in glob.glob(f"data/out/{width}x{height}/{self.fragment_type}/{episode_id}/*.avi") ]
        # Split data between workers
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:  # single-process data loading, return the full iterator
            first = 0
            last = len(paths)
        else:
            per_worker = int(math.ceil(len(paths) / float(worker_info.num_workers)))
            worker_id = worker_info.id
            first = worker_id * per_worker
            last = min(first + per_worker, len(paths))
            logging.info(f"Workerid: {worker_id}; [{first}:{last}]")
        for path in paths[first:last]:
            with m.VideoFileClip(path) as video:
            #logging.info(f"Path: {path}, size: {video.size}")
                if self.duration is None:
                    i = os.path.splitext(os.path.basename(path))[0]
                    meta = json.load(open(f"{os.path.dirname(path)}/{i}.json"))
                    clips = pig.preprocess.lines(video, meta)
                else:
                    clips = pig.preprocess.segment(video, duration=self.duration, jitter=self.jitter)
                for clip in clips:
                    yield clip
                                       

    def _positives(self, items):
        clips  = list(enumerate(items))
        for i, a in clips:
            for j, b in clips:
                if j == i:
                #if abs(j - i) <= self.window:
                    yield Pair(video = a.video, audio = b.audio, video_idx = i, audio_idx = j)
                    

    def __iter__(self):
        for _path, items in groupby(self._clips(), key=lambda x: x.filename):
            yield from self._positives(items)                    

@dataclass
class Stats:
    """Mean and standard deviation of a data sample."""
    video_mean : torch.Tensor
    video_std  : torch.Tensor
    audio_mean : torch.Tensor
    audio_std  : torch.Tensor
            
def get_stats(loader):
    """Compute means and standard deviations over data points from `loader`."""
    # Mean pass
    video_sum = torch.zeros(1,3,1,1,1).float()
    video_count = torch.zeros(1,3,1,1,1).float()
    audio_sum = torch.zeros(1,1,1).float()
    audio_count = torch.zeros(1,1,1).float()
    for batch in loader:
         video_sum   += batch.video.sum(dim=(0,2,3,4), keepdim=True)
         video_count += torch.ones_like(batch.video).sum(dim=(0,2,3,4), keepdim=True)
         audio_sum   += batch.audio.sum(dim=(0,2), keepdim=True) 
         audio_count += torch.ones_like(batch.audio).sum(dim=(0,2), keepdim=True)
    video_mean = video_sum/video_count
    audio_mean = audio_sum/audio_count

    # STD pass
    video_sse = torch.zeros(1,3,1,1,1).float()
    audio_sse = torch.zeros(1,1,1).float()
    for batch in loader:
        video_sse += ((batch.video - video_mean)**2).sum(dim=(0,2,3,4), keepdim=True)
        audio_sse += ((batch.audio - audio_mean)**2).sum(dim=(0,2), keepdim=True)
    return Stats(video_mean = video_mean.squeeze(),
                 video_std  = ((video_sse/video_count) **0.5).squeeze(),
                 audio_mean = audio_mean.squeeze(),
                 audio_std  = ((audio_sse/audio_count) **0.5).squeeze())

def worker_init_fn(worker_id):
    raise NotImplemented

def config_id(config):
    import hashlib
    sha = hashlib.sha256()
    sha.update(json.dumps(config).encode())
    return sha.hexdigest()
    
class PigData(pl.LightningDataModule):

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.loader_args = ['batch_size', 'shuffle']
        if self.config['iterable']:
            self.Dataset = lambda *args, **kwargs: PeppaPigIterableDataset(*args, **kwargs)
        else:
            self.Dataset = lambda *args, **kwargs: PeppaPigDataset(cache=self.config['cache'], *args, **kwargs)
    
    def prepare_data(self):
        if self.config['extract']:
            logging.info("Extracting data")
            pig.preprocess.extract()
        if self.config['prepare']:    
            logging.info("Collecting stats on training data.")
            
            train = self.Dataset(target_size=self.config['target_size'],
                                 split=['train'], fragment_type='dialog', 
                                  **{k:v for k,v in self.config['train'].items()
                                     if k not in self.loader_args})
            logging.info("Saving stats")
            stats = get_stats(DataLoader(train, collate_fn=collate, batch_size=32))
            torch.save(stats, "data/out/stats.pt")

    def setup(self, **kwargs):
        if self.config['normalization'] == 'peppa':
            self.stats = torch.load("data/out/stats.pt")
        elif self.config['normalization'] == 'kinetics':
            self.stats = torch.load("data/out/kinetics-stats.pt")
        else:
            raise ValueError(f"Unsupported normalization type {self.normalization}")
        self.transform = Compose([
            pig.transforms.SwapCT(),
            Normalize(mean=self.stats.video_mean, std=self.stats.video_std),    
            pig.transforms.SwapCT(),
            ])

        logging.info("Creating train/val/test datasets")
        self.train = self.Dataset(target_size=self.config['target_size'],
                                  transform=self.transform,
                                  split=['train'], fragment_type='dialog', 
                                  **{k:v for k,v in self.config['train'].items()
                                     if k not in self.loader_args})

        self.val_dia   = PeppaPigDataset(cache=self.config['cache'],
                                         transform=self.transform,
                                         target_size=self.config['target_size'],
                                         split=['val'], fragment_type='dialog',
                                         duration=3.2,
                                         **{k:v for k,v in self.config['val'].items()
                                            if k not in self.loader_args})

        if self.config['fixed_triplet']:
            self.val_dia3 = PeppaPigDataset.from_directory("data/out/val_dialog_triplets")
        else:
            self.val_dia3 = PeppaPigDataset(cache=self.config['cache'],
                                            transform=self.transform,
                                            target_size=self.config['target_size'],
                                            triplet=True,
                                            split=['val'], fragment_type='dialog', duration=None,
                                            **{k:v for k,v in self.config['val'].items()
                                               if k not in self.loader_args})
        self.val_narr = PeppaPigDataset(cache=self.config['cache'],
                                        transform=self.transform,
                                        target_size=self.config['target_size'],
                                        triplet=False,
                                        split=['val'], fragment_type='narration',
                                        duration=3.2,
                                        **{k:v for k,v in self.config['val'].items()
                                           if k not in self.loader_args})
        if self.config['fixed_triplet']:
            self.val_narr3 = PeppaPigDataset.from_directory("data/out/val_narration_triplets")
        else:
            self.val_narr3 = PeppaPigDataset(cache=self.config['cache'],
                                             transform=self.transform,
                                             target_size=self.config['target_size'],
                                             triplet=True,
                                             split=['val'], fragment_type='narration', duration=None,
                                             **{k:v for k,v in self.config['val'].items()
                                                if k not in self.loader_args})
            

    def train_dataloader(self):
        return DataLoader(self.train, collate_fn=collate, num_workers=self.config['num_workers'],
                          batch_size=self.config['train']['batch_size'],
                          shuffle=self.config['train']['shuffle'])

    def val_dataloader(self):
        
        dia = DataLoader(self.val_dia, collate_fn=collate, num_workers=self.config['num_workers'],
                          batch_size=self.config['val']['batch_size'])
        narr = DataLoader(self.val_narr, collate_fn=collate,
                               num_workers=self.config['num_workers'],
                          batch_size=self.config['val']['batch_size'])
        dia3 = DataLoader(self.val_dia3, collate_fn=collate_triplets,
                             num_workers=self.config['num_workers'],
                             batch_size=self.config['val']['batch_size'])
        narr3 = DataLoader(self.val_narr3, collate_fn=collate_triplets,
                             num_workers=self.config['num_workers'],
                             batch_size=self.config['val']['batch_size'])
        
        return [ dia, dia3, narr, narr3 ]
    
    def test_dataloader(self):
        raise NotImplementedError
        #return DataLoader(self.test, collate_fn=collate, num_workers=self.config['num_workers'],
        #                  batch_size=self.config['test']['batch_size'])

def pairs(xs):
    if len(xs) < 2:
        return []
    else:
        return [(xs[0], xs[1])] + pairs(xs[2:])

@dataclass
class Triplet:
    anchor: ...
    positive: ...
    negative: ...
    
    def __hash__(self):
        return hash((self.anchor, self.positive, self.negative))
    

@dataclass
class TripletBatch:
    anchor: ...
    positive: ...
    negative: ...

    def __hash__(self):
        return hash((self.anchor.sum(), self.positive.sum(), self.negative.sum()))
    


def _triplets(clips, criterion): 
    for size, items in grouped(clips, key=criterion):
        paired = pairs(shuffled(items))
        for p in paired:
            target, distractor = random.sample(p, 2)
            yield (target, distractor)


def triplets(clips):
    """Generates triplets of (a, v1, v2) where a is an audio clip, v1
       matching video and v2 a distractor video, matched by duration."""
    items = _triplets(clips, lambda x: x.duration)
    for target, distractor in items:
        yield Triplet(anchor=target.audio, positive=target.video, negative=distractor.video)


def collate_triplets(data):
    anchor, pos, neg = zip(*[(x.anchor, x.positive, x.negative) for x in data])
    return TripletBatch(anchor=pad_audio_batch(anchor),
                        positive=pad_video_batch(pos),
                        negative=pad_video_batch(neg))


def shuffled(xs):
    return sorted(xs, key=lambda _: random.random())

def grouped(xs, key=lambda x: x):
    return groupby(sorted(xs, key=key), key=key)
