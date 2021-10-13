import torch
from torch.utils.data import Dataset, IterableDataset, DataLoader
from torchvision.transforms import Normalize, Compose
import pig.transforms 
from dataclasses import dataclass
import glob
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

@dataclass
class Clip:
    """Video clip with associated audio."""
    video: torch.tensor
    audio: torch.tensor
    duration: float
    filename: str
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

    
class PeppaPigIDataset(Dataset):
    def __init__(self):
        raise NotImplemented

    def __len__(self):
        raise NotImplemented

    def __getitem__(self, idx):
        raise NotImplemented


class PeppaPigIterableDataset(IterableDataset):
    def __init__(self,
                 split='val',
                 fragment_type='dialog',
                 window=0,
                 transform=None,
                 duration=3.2,
                 raw=False,
                 triplet=False):
        self.split = split
        self.fragment_type = fragment_type
        self.window = window
        self.splits = dict(train = range(1, 197),
                           val  = range(197, 203),
                           test = range(203, 210))
        if transform is None:
            self.transform = pig.util.identity
        else:
            self.transform = transform
        self.duration = duration
        self.raw = raw
        self.triplet = triplet
        
    def _clips(self):
        for episode_id in self.splits[self.split]:
            for path in glob.glob(f"data/out/{self.fragment_type}/{episode_id}/*.avi"):
                with m.VideoFileClip(path) as video:
                    logging.info(f"Path: {path}, size: {video.size}")
                    if self.duration is None:
                        i = os.path.splitext(os.path.basename(path))[0]
                        meta = json.load(open(f"data/out/{self.fragment_type}/{episode_id}/{i}.json"))
                        clips = pig.preprocess.lines(video, meta)
                    else:
                        clips = pig.preprocess.segment(video, duration=self.duration)
                    for clip in clips:
                        if self.raw:
                            yield clip
                        else:
                            v = torch.stack([ torch.tensor(frame/255).float()
                                          for frame in clip.iter_frames() ])
                            a = torch.tensor(clip.audio.to_soundarray()).float()
                            yield Clip(video = self.transform(v.permute(3, 0, 1, 2)),
                                       audio = a.mean(dim=1, keepdim=True).permute(1,0),
                                       duration = clip.duration,
                                       filename = path)
                                       
                                       

    def _positives(self, items):
        clips  = list(enumerate(items))
        for i, a in clips:
            for j, b in clips:
                if abs(j - i) <= self.window:
                    if self.raw:
                        yield RawPair(video=a, audio=b.audio, video_idx = i, audio_idx = j)
                    else:
                        yield Pair(video = a.video, audio = b.audio, video_idx = i, audio_idx = j)                          
    def __iter__(self):
        if self.triplet:
            clips = list(self._clips())
            yield from triplets(clips, raw=self.raw)
        else:
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



class PigData(pl.LightningDataModule):

    def __init__(self, config, extract=False, prepare=False):
        super().__init__()
        self.extract = extract
        self.prepare = prepare
        self.config = config
    
    def prepare_data(self):
        if self.extract:
            logging.info("Extracting data")
            pig.preprocess.extract()
        if self.prepare:    
            logging.info("Collecting stats on training data.")
            train = PeppaPigIterableDataset(split=self.config['train']['split'],
                                            fragment_type=self.config['train']['fragment_type'],
                                            window=self.config['train']['window'],
                                            transform=self.config['transform'])
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
        self.train = PeppaPigIterableDataset(transform=self.config['transform'],
                                             **{k:v for k,v in self.config['train'].items()
                                                if k != 'batch_size'})
        self.val_main   = PeppaPigIterableDataset(transform=self.config['transform'],
                                             **{k:v for k,v in self.config['val'].items()
                                                if k != 'batch_size'})
        self.val_triplet = PeppaPigIterableDataset(transform=self.config['transform'],
                                                    triplet=True,
                                                    **{k:v for k,v in self.config['val'].items()
                                                       if k != 'batch_size'})
        self.test  = PeppaPigIterableDataset(transform=self.config['transform'],
                                             **{k:v for k,v in self.config['test'].items()
                                                if k != 'batch_size'})
        

    def train_dataloader(self):
        return DataLoader(self.train, collate_fn=collate,
                          batch_size=self.config['train']['batch_size'])

    def val_dataloader(self):
        
        main = DataLoader(self.val_main, collate_fn=collate,
                          batch_size=self.config['val']['batch_size'])
        triplet = DataLoader(self.val_triplet, collate_fn=collate_triplets,
                             batch_size=self.config['val']['batch_size'])
        
        return [ main, triplet ]
    
    def test_dataloader(self):
        return DataLoader(self.test, collate_fn=collate,
                          batch_size=self.config['test']['batch_size'])

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

@dataclass
class TripletBatch:
    anchor: ...
    positive: ...
    negative: ...
    

    
def triplets(clips, raw=False):
    """Generates triplets of (a, v1, v2) where a is an audio clip, v1
       matching video and v2 a distractor video, matched by duration."""
    for size, items in groupby(clips, key=lambda x: x.duration):
        logging.info(f"Pairing clips with duration {size}")
        paired = pairs(sorted(items, key=lambda _: random.random()))
        for p in paired:
            target, distractor = random.sample(p, 2)
            if raw:
                yield Triplet(anchor=target.audio, positive=target, negative=distractor)
            else:
                yield Triplet(anchor=target.audio, positive=target.video, negative=distractor.video)

    
def collate_triplets(data):
    anchor, pos, neg = zip(*[(x.anchor, x.positive, x.negative) for x in data])
    return TripletBatch(anchor=pad_audio_batch(anchor),
                        positive=pad_video_batch(pos),
                        negative=pad_video_batch(neg))

