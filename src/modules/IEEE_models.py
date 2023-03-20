import torch
import scipy.io
import mne
import sklearn
import os 
import time
import random
import time
import scipy.linalg
import pytorch_lightning as pl
import matplotlib.pyplot as plt
import numpy as np
import lightgbm as lgb

from itertools import chain, product
import pickle # to write results to file

from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.model_selection import cross_val_score
from sklearn.ensemble import RandomForestClassifier
from sklearn.pipeline import Pipeline
from sklearn.metrics import accuracy_score
from mne_features.feature_extraction import FeatureExtractor
from torch.utils.data import random_split, DataLoader, Dataset
from torch.nn import functional as F
from torch import nn
from pytorch_lightning.core.module import LightningModule
from pytorch_lightning.loggers import TensorBoardLogger
from scipy.stats import norm, wasserstein_distance
from torchmetrics.classification import BinaryAccuracy

from .utils import *
from modules.properties import IEEE_properties as props

import warnings
warnings.filterwarnings("ignore", category=np.VisibleDeprecationWarning) 
print('here')
# get_ipython().run_line_magic('load_ext', 'tensorboard')



# Assess whether GPU is availble
if torch.cuda.is_available():
    print("PyTorch is using the GPU.")
    print("Device name - ", torch.cuda.get_device_name(torch.cuda.current_device()))
else: 
    print("PyTorch is not using the GPU.")
    

# ### Datset and Model classes

class convolution_AE(LightningModule):
    def __init__(self, input_channels, days_labels_N, task_labels_N, learning_rate=1e-3, filters_n = [32, 16, 4], mode = 'supervised'):
        super().__init__()
        self.input_channels = input_channels
        self.filters_n = filters_n
        self.learning_rate = learning_rate
        self.float()
        self.l1_filters, self.l2_filters, self.l3_filters = self.filters_n
        self.mode = mode
        self.switcher = True
        ### The model architecture ###
        

        # Encoder
        self.encoder = nn.Sequential(
        nn.Conv1d(self.input_channels, self.l1_filters, kernel_size=25, stride=5, padding=props['enoder_pad'][0]),
#         nn.Dropout1d(p=0.2),
#         nn.MaxPool1d(kernel_size=15, stride=3),
        nn.LeakyReLU(),
#         nn.AvgPool1d(kernel_size=2, stride=2),
        nn.Conv1d(self.l1_filters, self.l2_filters, kernel_size=10, stride=2, padding=props['enoder_pad'][1]),
#         nn.Dropout1d(p=0.2),
        nn.LeakyReLU(),
#         nn.AvgPool1d(kernel_size=2, stride=2),
        nn.Conv1d(self.l2_filters, self.l3_filters, kernel_size=5, stride=2, padding=props['enoder_pad'][2]),
#         nn.Dropout1d(p=0.2),
        nn.LeakyReLU()
        )
                
        # Decoder
        self.decoder = nn.Sequential(
        # IMPORTENT - on the IEEE dataset - the output padding needs to be 1 in the row below -on CHIST-ERA its 1
        nn.ConvTranspose1d(self.l3_filters, self.l2_filters, kernel_size=5, stride=2,\
                            padding=props['decoder_pad'][0], output_padding=props['decoder_pad'][1]),
#         nn.Dropout1d(p=0.33),
        nn.LeakyReLU(),
#         nn.Upsample(scale_factor=2, mode='linear'),
        nn.ConvTranspose1d(self.l2_filters, self.l1_filters, kernel_size=10, stride=2, \
                            padding=props['decoder_pad'][2], output_padding=props['decoder_pad'][3]),
#         nn.Dropout1d(p=0.33),
        nn.LeakyReLU(),
#         nn.Upsample(scale_factor=2, mode='linear'),
        nn.ConvTranspose1d(self.l1_filters, self.input_channels, kernel_size=25, stride=5, \
                            padding=props['decoder_pad'][4], output_padding=props['decoder_pad'][5]),
        )
        
        # Residuals Encoder
        self.res_encoder = nn.Sequential(
        nn.Conv1d(self.input_channels, self.l1_filters, kernel_size=25, stride=5, padding=props['enoder_pad'][0]),
        nn.LeakyReLU(),
        nn.Conv1d(self.l1_filters, self.l2_filters, kernel_size=10, stride=2, padding=props['enoder_pad'][1]),
        nn.LeakyReLU(),
        nn.Conv1d(self.l2_filters, self.l3_filters, kernel_size=5, stride=2, padding=props['enoder_pad'][2]),
        nn.LeakyReLU()
        )
                
        # Classifier Days
        self.classiffier_days = nn.Sequential(
        nn.Flatten(),
        nn.Linear(props['latent_sz'], days_labels_N),
        nn.Dropout(0.5),
        )
        
        # Classifier Task
        self.classiffier_task = nn.Sequential(
        nn.Flatten(),
        nn.Linear(props['latent_sz'], task_labels_N),
        nn.Dropout(0.5),

        )
        
      
    def forward(self, x):
        # Forward through the layeres
        # Encoder
        x = self.encoder(x)

        # Decoder
        x = self.decoder(x)
        return x
    
    def encode(self, x):
        # Forward through the layeres
        # Encoder
        x = self.encoder(x)
        return x
    
    def on_train_epoch_end(self):
        if self.current_epoch > 200:
            self.unfreeze_decoder()
            self.unfreeze_encoder()
            self.mode = 'all'
    
        if self.current_epoch % 20 == 0:
            self.switcher = not self.switcher
            if self.switcher == True:
                self.freeze_decoder()
                self.unfreeze_encoder()
                self.mode = 'task'
            elif self.switcher == False:
                self.freeze_encoder()
                self.unfreeze_decoder()
                self.mode = 'reconstruction'
        
    def training_step(self, batch, batch_idx):
        # Extract batch
        x, y, days_y = batch
        # Define loss functions
        loss_fn_days = nn.CrossEntropyLoss()
        loss_fn_rec = nn.MSELoss()
        loss_fn_task = nn.CrossEntropyLoss()
            
        # Encode
        encoded = self.encode(x)
        
        # Get predictions for task
        preds_task = self.classiffier_task(encoded)
        task_loss = loss_fn_task(preds_task, y)

        # Compute task classification accuracy
        task_acc = sklearn.metrics.accuracy_score(np.argmax(F.softmax(preds_task, dim=-1).detach().cpu().numpy(), axis=1),
                                             np.argmax(y.detach().cpu().numpy(), axis=1))

        # Log scalars
        self.log('task_loss', task_loss, prog_bar=True, on_step=False, on_epoch=True)
        self.log('task_accuracy', task_acc, prog_bar=True, on_step=False, on_epoch=True)

        # Decode
        reconstructed = self.decoder(encoded)

        # Compute residuals
        residuals = torch.sub(x, reconstructed)

        # Encode residuals
        residuals_compact = self.res_encoder(residuals)

        # Get predictions per day
        preds_days = self.classiffier_days(residuals_compact)

        # Compute all losses
        days_loss = loss_fn_days(preds_days, days_y)
        reconstruction_loss = loss_fn_rec(reconstructed, x)

        # Compute days classification accuracy
        days_acc = sklearn.metrics.accuracy_score(np.argmax(F.softmax(preds_days, dim=-1).detach().cpu().numpy(), axis=1),
                                             np.argmax(days_y.detach().cpu().numpy(), axis=1))

        # Log results
        self.log('days_loss', days_loss, prog_bar=True, on_step=False, on_epoch=True)
        self.log('reconstruction_loss', reconstruction_loss, prog_bar=True, on_step=False, on_epoch=True)
        self.log('days_accuracy', days_acc, prog_bar=True, on_step=False, on_epoch=True)

        if self.mode == 'task':
            return days_loss + task_loss
        elif self.mode == 'reconstruction':
            return reconstruction_loss
        elif self.mode == 'all':
            return reconstruction_loss + days_loss + task_loss
   
    def get_lr(optimizer):
        for param_group in optimizer.param_groups:
            return param_group['lr']
    
    
    def freeze_encoder(self):
        for name, param in self.encoder.named_parameters():
            param.requires_grad = False
            
    def unfreeze_encoder(self):
        for name, param in self.encoder.named_parameters():
            param.requires_grad = True
            
    def freeze_decoder(self):
        for name, param in self.decoder.named_parameters():
            param.requires_grad = False
            
    def unfreeze_decoder(self):
        for name, param in self.decoder.named_parameters():
            param.requires_grad = True
            
            
    def change_mode(self, mode):
        self.mode = mode
        
        
    def configure_optimizers(self):
        # Optimizer
        return torch.optim.Adam(self.parameters(), lr=self.learning_rate)
    







