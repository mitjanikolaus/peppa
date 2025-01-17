import argparse

import torch
import glob
from pig.models import PeppaPig
import pig.data
import pytorch_lightning as pl
import logging
from torch.utils.data import DataLoader
from dataclasses import dataclass
import pandas as pd
import numpy as np
import torch
import random
import yaml
from copy import deepcopy

random.seed(666)
torch.manual_seed(666)

BATCH_SIZE=8

def data_statistics():
    rows = []
    for split in ['train', 'val', 'test']:
        for fragment_type in ['dialog', 'narration']:
            if pig.data.SPLIT_SPEC[fragment_type][split] is not None:
                ds = pig.data.PeppaPigIterableDataset(
                    target_size=(180, 100),
                    split=[split],
                    fragment_type=fragment_type,
                    duration=2.3)
                duration = np.array([clip.duration for clip in ds._raw_clips() ])
                rows.append({'Split': split, 'Type': fragment_type, 
                             'Size (h)': duration.sum() / 60 / 60,
                             '# Clips': len(duration)})
    data = pd.DataFrame.from_records(rows)
    data.to_csv("results/data_statistics.csv", index=False, header=True)
    data.to_latex("results/data_statistics.tex", index=False, header=True, float_format="%.2f")
    

def load_best_model(dirname, higher_better=True):
    info = []
    for path in glob.glob(f"{dirname}/checkpoints/*.ckpt"):
       cp = torch.load(path, map_location='cpu')
       item = cp['callbacks'][pl.callbacks.model_checkpoint.ModelCheckpoint]
       if item['best_model_score'] is not None:
           info.append(item)
    best = sorted(info, key=lambda x: x['best_model_score'], reverse=higher_better)[0]
    logging.info(f"Best {best['monitor']}: {best['best_model_score']} at {best['best_model_path']}")
    local_model_path = best['best_model_path'].split("/peppa/")[1]
    net = PeppaPig.load_from_checkpoint(local_model_path, hparams_file=f"{dirname}/hparams.yaml")
    return net, best['best_model_path']

def score_means(data):
    rows = []
    for item in data:
        row = deepcopy(item)
        row['triplet_acc_std'] = row['triplet_acc'].std().item()
        row['triplet_acc'] = row['triplet_acc'].mean().item()
        row['recall_at_10_fixed_std'] = row['recall_at_10_fixed'].mean(dim=1).std().item()
        row['recall_at_10_fixed'] = row['recall_at_10_fixed'].mean(dim=1).mean().item()
        row['recall_at_10_jitter_std'] = row['recall_at_10_jitter'].mean(dim=1).std().item()
        row['recall_at_10_jitter'] = row['recall_at_10_jitter'].mean(dim=1).mean().item()
        rows.append(row)
    return pd.DataFrame.from_records(rows)

def full_score(model, gpus, split=['val']):
    """Compute all standard scores for the given model. """
    trainer = pl.Trainer(gpus=gpus, logger=False, precision=16)
    data = []
    if split == ['test']:
        types = ['narration']
    elif split ==['val']:
        types = ['dialog', 'narration']
    else:
        raise NotImplementedError
    for fragment_type in types:
        
        for scrambled_video in [False, True]:
            logging.info(f"Evaluating: {fragment_type}, scramble={scrambled_video} triplet")
            acc = triplet_score(fragment_type, model, trainer, scrambled_video=scrambled_video, split=split)
            logging.info(f"Evaluating: {fragment_type}, scramble={scrambled_video} recall_fixed")
            rec_fixed = resampled_retrieval_score(fragment_type,
                                                  model,
                                                  trainer,
                                                  duration=2.3,
                                                  jitter=False,
                                                  jitter_sd=None,
                                                  scrambled_video=scrambled_video,
                                                  split=split,
                                                  one_to_n=True)
            logging.info(f"Evaluating: {fragment_type}, scramble={scrambled_video} recall_jitter")
            rec_jitter = resampled_retrieval_score(fragment_type,
                                                   model,
                                                   trainer,
                                                   duration=2.3,
                                                   jitter=True,
                                                   jitter_sd=0.5,
                                                   scrambled_video=scrambled_video,
                                                   split=split,
                                                   one_to_n=True)
            data.append(dict(fragment_type=fragment_type,
                             scrambled_video=scrambled_video,
                             triplet_acc=acc,
                             recall_fixed=rec_fixed,
                             recall_jitter=rec_jitter,
                             recall_at_10_fixed=rec_fixed[:,10,:],
                             recall_at_10_jitter=rec_jitter[:,10,:]))
    return data
        
def retrieval_score(fragment_type, model, trainer, duration=2.3, jitter=False, jitter_sd=None, batch_size=BATCH_SIZE, split=['val']):
        base_ds = pig.data.PeppaPigDataset(
            target_size=model.config["data"]["target_size"],
            split=split,
            fragment_type=fragment_type,
            duration=duration,
            jitter=jitter,
            jitter_sd=jitter_sd
            )
        key = lambda x: x.audio_duration
        loader = pig.data.grouped_loader(base_ds, key, pig.data.collate, batch_size=batch_size)
        V, A = zip(* [(batch.video, batch.audio) for batch
                  in trainer.predict(model, loader) ])
        V = torch.cat(V, dim=0)
        A = torch.cat(A, dim=0)
        correct = torch.eye(V.shape[0], device=A.device)
        rec10 = pig.metrics.recall_at_n(V, A, correct=correct, n=10).mean().item()
        return rec10

def resampled_retrieval_score(fragment_type,
                              model,
                              trainer,
                              duration=2.3,
                              jitter=False,
                              jitter_sd=None,
                              batch_size=BATCH_SIZE,
                              scrambled_video=False,
                              split=['val'],
                              one_to_n=False
                              ):
        base_ds = pig.data.PeppaPigDataset(
            target_size=model.config["data"]["target_size"],
            split=split,
            fragment_type=fragment_type,
            duration=duration,
            audio_sample_rate=model.config["data"].get('audio_sample_rate',
                                                       pig.data.DEFAULT_SAMPLE_RATE),
            jitter=jitter,
            jitter_sd=jitter_sd,
            scrambled_video=scrambled_video,
            )
        key = lambda x: x.audio_duration
        loader = pig.data.grouped_loader(base_ds, key, pig.data.collate, batch_size=batch_size)
        V, A = zip(* [(batch.video, batch.audio) for batch
                  in trainer.predict(model, loader) ])
        V = torch.cat(V, dim=0)
        A = torch.cat(A, dim=0)
        rec = pig.metrics.resampled_recall_at_1_to_n(V, A, size=100, n_samples=500, N=10)
        if one_to_n:
            return rec
        else:
            return rec[:,10,:]


def triplet_score(fragment_type, model, trainer, batch_size=BATCH_SIZE, scrambled_video=False, split=['val']):
    from pig.triplet import TripletScorer
    scorer = TripletScorer(fragment_type=fragment_type, split=split, target_size=model.config["data"]["target_size"],
                           audio_sample_rate=model.config["data"].get('audio_sample_rate',
                                                                      pig.data.DEFAULT_SAMPLE_RATE),
                           scrambled_video=scrambled_video)
    acc = scorer.evaluate(model, trainer=trainer, n_samples=500, batch_size=batch_size)
    return acc

def comparative_triplet_score(fragment_type, models, trainer, batch_size=BATCH_SIZE,
                              scrambled_video=False, split=['val']):
    from pig.triplet import TripletScorer, comparative_score_triplets
    scorers = [ TripletScorer(fragment_type=fragment_type, split=split,
                            target_size=model.config["data"]["target_size"],
                            audio_sample_rate=model.config["data"].get('audio_sample_rate',
                                                                pig.data.DEFAULT_SAMPLE_RATE),
                            scrambled_video=scrambled_video)
                for model in models ]
    for i in range(len(models)):
        scorers[i]._encode(models[i], trainer, batch_size)
    result = comparative_score_triplets([ scorer._video for scorer in scorers],
                                        [ scorer._audio for scorer in scorers],
                                        scorers[0]._duration,
                                        n_samples=500)
    return result
    
def pretraining(row):
    return { (True, True): "AV",
             (True, False): "A",
             (False, True): "V",
             (False, False): "None"}[row['audio_pretrained'],
                                     row['video_pretrained']]

def format():
    data = torch.load("results/full_scores.pt")
    data = add_condition(data)
    data = score_means(data)
    for fragment_type in ['dialog', 'narration']:
        table = data.query(f"fragment_type=='{fragment_type}'")
        table['pretraining'] = pd.Categorical(table.apply(pretraining, axis=1),
                                              categories=['AV', 'A', 'V', 'None'])


        table[['version', 'static', 'jitter', 'pretraining', 'resolution',
               'recall_at_10_fixed', 'recall_at_10_jitter', 'triplet_acc']]\
            .sort_values(by=['static', 'jitter', 'pretraining', 'resolution'])\
            .replace(True, "Yes").replace(False, "")\
            .rename(columns=dict(version='ID',
                                 static='Static',
                                 jitter='Jitter',
                                 pretraining='Pretraining',
                                 resolution='Resolution',
                                 recall_at_10_fixed='R@10 (fixed)',
                                 recall_at_10_jitter='R@10 (jitter)',
                                 triplet_acc='Triplet Acc'))\
            .to_latex(buf=f"results/scores_{fragment_type}.tex",
                      index=False,
                      float_format="%.3f")


def add_condition(data):
    rows = []
    for row in data:
        record = {k:v for k,v in row.items()}
        config = yaml.safe_load(open(row['hparams_path']))
        record['jitter'] = config['data']['train']['jitter']
        record['static'] = config['video'].get('static', False)
        record['audio_pretrained'] = config['audio']['pretrained']
        record['video_pretrained'] = config['video']['pretrained']
        record['resolution'] = 'x'.join(map(str, config['data']['target_size']))
        record['freeze_wav2vec'] = config['audio']['freeze_feature_extractor'] \
            and config['audio']['freeze_encoder_layers'] == 12
        record['sample_rate'] = str(config['data'].get('audio_sample_rate',
                                                       pig.data.DEFAULT_SAMPLE_RATE))
        rows.append(record)
    return rows


def full_run(versions = None, gpus=1):
    if versions is None:
        conditions = yaml.safe_load(open("conditions.yaml"))
        versions = [ version for value in conditions.values() for version in value ]
    logging.getLogger().setLevel(logging.INFO)
    for version in versions:
        rows = []
        logging.info(f"Evaluating version {version}")
        net, path = load_best_model(f"lightning_logs/version_{version}/")
        for row in full_score(net, gpus=gpus, split=['val']):
            row['version']         = version
            row['checkpoint_path'] = path
            row['hparams_path']    = f"lightning_logs/version_{version}/hparams.yaml"
            rows.append(row)
        torch.save(add_condition(rows), f"results/full_scores_v{version}.pt")
    


def test_run(gpu=0):
    conditions = yaml.safe_load(open("conditions.yaml"))
    rows = []
    for version in conditions['base']:
        logging.info(f"Evaluating version {version}")
        net, path = load_best_model(f"lightning_logs/version_{version}/")
        for row in full_score(net, gpus=[gpu], split=['test']):
            row['version']         = version
            row['checkpoint_path'] = path
            row['hparams_path']    = f"lightning_logs/version_{version}/hparams.yaml"
            rows.append(row)
    torch.save(add_condition(rows), f"results/full_test_scores.pt")
    
def test_table():
    data = torch.load(f"results/full_test_scores.pt")
    rows = [ datum for datum in data if not datum['scrambled_video'] ]
    recall_fixed  = torch.cat([ row['recall_at_10_fixed'].mean(dim=1) for row in rows ])
    recall_jitter = torch.cat([ row['recall_at_10_jitter'].mean(dim=1) for row in rows ])
    triplet_acc   = torch.cat([ row['triplet_acc']  for row in rows ])
    table = pd.DataFrame.from_records(
        [{'R@10 (fixed)':
          f"{recall_fixed.mean().item():0.2f} ± {recall_fixed.std().item():0.2f}",
          'R@10 (jitter)':
          f"{recall_jitter.mean().item():0.2f} ± {recall_jitter.std().item():0.2f}",
          'Triplet Acc':
           f"{triplet_acc.mean().item():0.2f} ± {triplet_acc.std().item():0.2f}"}]).\
          to_latex(buf=f"results/scores_test.tex", index=False)

def duration_effect(gpu=0):
    conditions = yaml.safe_load(open("conditions.yaml"))
    model_id1 = conditions['pretraining_a']
    model_id2 = conditions['static']
    out = []
    models = []
    for model_id in model_id1 + model_id2:
        logging.info(f"Loading version {model_id}")
        model, _ = load_best_model(f"lightning_logs/version_{model_id}/")
        models.append(model)
    trainer = pl.Trainer(gpus=[gpu], logger=False, precision=16)
    for fragment_type in ['dialog', 'narration']:
        logging.info(f"Comparing for {fragment_type}")
        result = comparative_triplet_score(fragment_type,
                                           models,
                                           trainer=trainer,
                                           scrambled_video=False,
                                           split=['val'])
        result['fragment_type'] = fragment_type
        result['model_ids'] = model_id1 + model_id2
        out.append(result)
    torch.save(out, "results/duration_effect.pt")
        
