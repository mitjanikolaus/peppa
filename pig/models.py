import os
import torch
from torch import nn
import torch.nn.functional as F
from torchvision import transforms
from torch.utils.data import DataLoader, random_split
import pytorch_lightning as pl
import torchvision.models.video as V
import torchaudio.models as A
from torchaudio.models.wav2vec2.utils import import_fairseq_model
import fairseq
from torchvision.transforms import Compose
from pig.loss import TripletLoss
import pig.data
import pig.triplet
import logging
import sys
import pig.util
import pig.metrics
from pytorch_lightning.callbacks import ModelCheckpoint
import pig.optimization as opt
import pig.transforms
from torchvision.transforms import Normalize, Compose

class Attention(nn.Module):
    def __init__(self, in_size, hidden_size):
        super().__init__()
        self.hidden = nn.Linear(in_size, hidden_size)
        self.out = nn.Linear(hidden_size, in_size)
        self.softmax = nn.Softmax(dim=1)

    def forward(self, input):
        # calculate the attention weights
        alpha = self.softmax(self.out(torch.tanh(self.hidden(input))))
        # apply the weights to the input and sum over all timesteps
        x = (alpha * input).sum(dim=1)
        # return the resulting embedding
        return x

class AveragePool(nn.Module):
    def __init__(self, size=512):
        super().__init__()
        self.pool = torch.nn.AdaptiveAvgPool2d((size, 1))

    def forward(self, x):
        return self.pool(x).squeeze(dim=2)

    
class LastStep(nn.Module):
    """Use the last time-step of the audio encoder and the embedding. """
    # This is supposed to work similar to the use of the [CLS] token in BERT
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x[:,-1, :]
        
    
## Audio encoders

class Wav2VecEncoder(nn.Module):
    def __init__(self, path, pretrained=True, freeze_feature_extractor=False, freeze_encoder_layers=None, pooling='average'):
        super().__init__()
        if pretrained:
            model, _, _ = fairseq.checkpoint_utils.load_model_ensemble_and_task([path])
            self.audio = import_fairseq_model(model[0], num_out=28)
        else:
            self.audio = A.wav2vec2_base(num_out=28)
        if freeze_feature_extractor:
            for param in self.audio.feature_extractor.parameters():
                param.requires_grad = False
        if freeze_encoder_layers is not None:
            for index in range(0, freeze_encoder_layers):
                for param in self.audio.encoder.transformer.layers[index].parameters():
                    param.requires_grad = False
        if pooling == 'average':
            self.audiopool = AveragePool(size=512)
        elif pooling == 'attention':
            self.audiopool = Attention(512, 128)
        elif pooling == 'last':
            self.audiopool = LastStep()
        else:
            raise ValueError(f"Invalid pooling: {pooling}")
        self.project = nn.Linear(512, 512)

        
    def forward(self, x):
        features, _ = self.audio.extract_features(x.squeeze(dim=1))
        return Compose([self.audiopool,
                        self.project,
                        lambda x: nn.functional.normalize(x, p=2, dim=1)
        ])(features)

        
## Video encoders
class R3DEncoder(nn.Module):
    
    def __init__(self,
                 pretrained=True,
                 project=True,
                 version='r3d_18',
                 pooling='average'):
        super().__init__()
        self.pretrained = pretrained
        if version == 'r3d_18':
            self.video = V.r3d_18(pretrained=pretrained, progress=False)
        elif version == 'mc3_18':
            self.video = V.mc3_18(pretrained=pretrained, progress=False)
        elif version == 'r2plus1d_18':
            self.video = V.r2plus1d_18(pretrained=pretrained, progress=False)
        else:
            raise ValueError(f"Invalid version {version}")
        if project:
            self.project = nn.Linear(512, 512)
        else:
            self.project = identity
        if pooling == 'attention':
            self.videopool = VideoAttention(512, 128)
        elif pooling == 'average':
            self.videopool = VideoAveragePool()
        else:
            raise ValueError(f"Invalid pooling {pooling}")
        self.transform = build_transform("kinetics" if self.pretrained else "peppa")
        
    def forward(self, x):
        return Compose([self.transform,
                        self.video.stem,
                        self.video.layer1,
                        self.video.layer2,
                        self.video.layer3,
                        self.video.layer4,
                        self.videopool,
                        self.project,
                        lambda x: nn.functional.normalize(x, p=2, dim=1)
        ])(x)

class VideoAveragePool(nn.Module):

    def __init__(self):
        super().__init__()
        self.pool = torch.nn.AdaptiveAvgPool3d(output_size=(1, 1, 1))

    def forward(self, x):
        return self.pool(x).flatten(1)

class VideoAttention(nn.Module):

    def __init__(self, in_size=512, hidden_size=128):
        super().__init__()
        self.spatial_avg = torch.nn.AdaptiveAvgPool2d(output_size=(1, 1))
        self.attn = Attention(in_size, hidden_size)

    def forward(self, x):
        return self.attn(self.spatial_avg(x).flatten(2).permute(0, 2, 1))
    
class PeppaPig(pl.LightningModule):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.save_hyperparameters(config)
        self.loss = TripletLoss(margin=self.config['margin'])
        self.video_encoder = R3DEncoder(**self.config['video'])
        self.audio_encoder = Wav2VecEncoder(**config['audio'])
        
    def forward(self, batch):
        # in lightning, forward defines the prediction/inference actions
        try:
            a = self.encode_audio(batch.anchor)
            p = self.encode_video(batch.positive)
            n = self.encode_video(batch.negative)
            return pig.triplet.TripletBatch(anchor=a, positive=p, negative=n)
        except AttributeError:
            V = self.encode_video(batch.video)
            A = self.encode_audio(batch.audio)
            return pig.data.ClipBatch(video=V, audio=A)
        
    def encode_video(self, x):
        return self.video_encoder(x)
    
    def encode_audio(self, x):
        return self.audio_encoder(x)
    
    def training_step(self, batch, batch_idx):
        # training_step defined the train loop.
        # It is independent of forward
        
        V = self.encode_video(batch.video)
        A = self.encode_audio(batch.audio)
        loss = self.loss(V, A)
        # Logging to TensorBoard by default
        self.log("train_loss", loss, prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx, dataloader_idx=None):
        if dataloader_idx == 0:
            V = self.encode_video(batch.video)
            A = self.encode_audio(batch.audio)
            loss = self.loss(V, A)
            # Logging to TensorBoard by default
            self.log("val_loss", loss, prog_bar=True)
            return (V, A)
        elif dataloader_idx == 1:
            V = self.encode_video(batch.video)
            A = self.encode_audio(batch.audio)
            loss = self.loss(V, A)
            # Logging to TensorBoard by default
            self.log("valnarr_loss", loss, prog_bar=False)
            return (V, A)
        else:
            raise ValueError(f"Invalid dataloader index {dataloader_idx}")
        
        
    def validation_epoch_end(self, outputs):
        out_main, out_narr = outputs
        V, A = zip(*out_main)
        V = torch.cat(V, dim=0)
        A = torch.cat(A, dim=0)
        correct = torch.eye(V.shape[0], device=A.device)
        rec10 = pig.metrics.recall_at_n(V, A, correct=correct, n=10)
        self.log("val_rec10", rec10, prog_bar=True)
        V, A = zip(*out_narr)
        V = torch.cat(V, dim=0)
        A = torch.cat(A, dim=0)
        correct = torch.eye(V.shape[0], device=A.device)
        rec10 = pig.metrics.recall_at_n(V, A, correct=correct, n=10)
        self.log("valnarr_rec10", rec10, prog_bar=True)

        
        
    #def test_step(self, batch, batch_idx):    
    
    def configure_optimizers(self):
        #optimizer = torch.optim.Adam(self.parameters(), **self.config['optimizer'])
        optimizer = opt.BertAdam(self.parameters(), **self.config['optimizer'])
        return optimizer

def build_transform(normalization):
    if normalization == 'peppa':
        stats = torch.load("data/out/stats.pt")
    elif normalization == 'kinetics':
        stats = torch.load("data/out/kinetics-stats.pt")
    else:
        raise ValueError(f"Unsupported normalization type {self.normalization}")
    return Compose([
        pig.transforms.SwapCT(),
        Normalize(mean=stats.video_mean, std=stats.video_std, inplace=True),
        pig.transforms.SwapCT()
    ])
