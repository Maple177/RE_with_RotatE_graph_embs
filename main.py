from __future__ import absolute_import, division, print_function

import logging
import os
import random

import numpy as np
import pandas as pd
import torch
from torch.optim import Adam, AdamW
from transformers import (BertConfig, BertTokenizer, get_linear_schedule_with_warmup)
from bert_model import BertForSequenceClassification

from tqdm import tqdm, trange
from sklearn.metrics import f1_score
from opt import get_args
from loader import DataLoader

logger = logging.getLogger(__name__)
embedding_weight_list = ["kb_embs.weight","rel_embs"]

def one_hot(x,num_labels):
    res = np.zeros((len(x),num_labels),dtype=np.int)
    for i, v in enumerate(x):
        res[i,v] = 1
    return res

def evaluate(dataloader,model,num_labels,eval=False,predict_only=False):
    eval_loss = 0.0
    nb_eval_steps = 0
    full_preds = []
    full_golds = [] # gold standard
    model.eval()
    for batch in dataloader:
        with torch.no_grad():
            if predict_only:
                logits = model(**batch)[0]
            else:
                loss, logits = model(**batch)[:2]
            preds = logits.detach().cpu().numpy()
            if not predict_only:
               eval_loss += loss.item()
               nb_eval_steps += 1
               preds = one_hot(np.argmax(preds,axis=1),num_labels)
               full_golds.append(batch["labels"].detach().cpu().numpy())
            else:
               preds = np.argmax(preds,axis=1)
            full_preds.append(preds)
    full_preds = np.concatenate(full_preds)
    if not predict_only and not eval:
        #train mode
        eval_loss = eval_loss / nb_eval_steps
        full_golds = np.concatenate(full_golds)
    if predict_only or eval:
        return full_preds
    else:
        return eval_loss, f1_score(full_golds,full_preds,average="micro",labels=list(range(1,num_labels)))
        
def train(args,train_dataloader,dev_dataloader,model,loading_info=None):
    """ Train the model """

    if not args.early_stopping:
         NUM_EPOCHS = args.num_train_epochs
    else:
         NUM_EPOCHS = args.max_num_epochs

    n_params = sum([p.nelement() for p in model.parameters()])
    print(f'* number of parameters: {n_params}')

    optimizer = AdamW(model.parameters(),lr=args.learning_rate)
    logger.info(f"learning rate: {args.learning_rate}")

    t_total = len(train_dataloader) * NUM_EPOCHS
    if args.warmup:
        scheduler = get_linear_schedule_with_warmup(optimizer,num_warmup_steps=args.warmup_ratio*t_total,num_training_steps=t_total)
        logger.info(f"use warmup: {int(args.warmup_ratio*100)} %  steps for warmup.")
    logger.info(f"number of epochs:{NUM_EPOCHS}; number of steps:{t_total}")
   
    best_model_dir = f"{args.mode}_{args.task_name}_{args.learning_rate}_{args.seed}/model"

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataloader))
    logger.info("  Num Epochs = %d", NUM_EPOCHS)
    logger.info("  Instantaneous batch size = %d", args.batch_size)

    global_step = 0
    logging_loss, min_loss, prev_dev_loss = 0.0, np.inf, np.inf
    max_score, prev_dev_score = -np.inf, -np.inf
    training_hist = []
    model.zero_grad()

    dev_loss_record = []
    dev_score_record = []

    for epoch in range(NUM_EPOCHS):
        tr_loss = 0.0
        logging_loss = 0.0
        grad_norm = 0.0
        
        for step, batch in enumerate(train_dataloader): 
            model.train()
            loss = model(**batch)[0] 

            loss.backward() # gradient will be stored in the network
            gnorm = torch.nn.utils.clip_grad_norm_(model.parameters(),args.max_grad_norm)

            grad_norm += gnorm
                                                
            tr_loss += loss.item()

            optimizer.step()
            if args.warmup:
                scheduler.step()
            optimizer.zero_grad()
            global_step += 1

            if args.logging_steps > 0 and (step + 1) % args.logging_steps == 0:
                # Log metrics
                logger.info(f"training loss = {(tr_loss - logging_loss)/args.logging_steps} | global step = {global_step}")
                logging_loss = tr_loss

        dev_loss, dev_score = evaluate(dev_dataloader,model,args.num_labels)
        dev_loss_record.append(dev_loss)
        dev_score_record.append(dev_score)

        if args.warmup:
            logger.info(f"current lr = {scheduler.get_lr()[0]}")
        logger.info(f"validation loss = {dev_loss} | validation F1-score = {dev_score} | epoch = {epoch}")
        
        if args.monitor == "loss" and dev_loss < min_loss:
            min_loss = dev_loss
            best_epoch = epoch
            
            # save model
            output_dir = os.path.join(args.output_dir,best_model_dir)
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
            model.save_pretrained(output_dir)
            torch.save(args, os.path.join(output_dir,'training_args.bin'))
            logger.info("new best model! saved.")
        
        if args.monitor == "score" and dev_score > max_score:
            max_score = dev_score
            best_epoch = epoch

            # save model
            output_dir = os.path.join(args.output_dir,best_model_dir)
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
            model.save_pretrained(output_dir)
            torch.save(args,os.path.join(output_dir,"training_args.bin"))
            logger.info("new best model! saved.")
        
        if args.early_stopping and args.monitor == "loss":
            if dev_loss < prev_dev_loss:
                training_hist.append(True)
            else:
                training_hist.append(False)
                if len(training_hist) > args.patience and not np.any(training_hist[-args.patience:]):
                    logger.info(f"early stopping triggered: best loss on validation set: {min_loss} at epoch {best_epoch}.")
                    break
            prev_dev_loss = dev_loss

        if args.early_stopping and args.monitor == "score":
            if dev_score >= prev_dev_score:
                training_hist.append(True)
            else:
                training_hist.append(False)
                if len(training_hist) > args.patience and not np.any(training_hist[-args.patience:]):
                    logger.info(f"early stopping triggered: best F-score on validation set: {max_score} at {best_epoch}.")
                    break
            prev_dev_score = dev_score

        if epoch + 1 == NUM_EPOCHS:
            break

    return output_dir, dev_loss_record, dev_score_record, best_epoch

def set_seed(args):
    seed = args.seed
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(seed)

def main():
    args = get_args()
    
    # Setup CUDA, GPU & distributed training
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    args.n_gpu = torch.cuda.device_count()
    args.device = device

    if not os.path.exists("logging"):
        os.makedirs("logging")
    if not os.path.exists("models"):
        os.makedirs("models")

    # Setup logging
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',datefmt='%m/%d/%Y %H:%S',level=logging.INFO,filename=f"logging/training_log_{args.run_id}",filemode='w')
    logger.warning("device: %s, n_gpu: %s",device, args.n_gpu)

    # Set seed
    #set_seed(args)

    # prepare model

    config = BertConfig.from_pretrained(args.config_name_or_path,num_labels=args.num_labels)
    
    output_dir = os.path.join(args.output_dir,f"{args.mode}_{args.task_name}_{args.learning_rate}_{args.seed}")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    train_dataloader = DataLoader(args,"train")
    dev_dataloader = DataLoader(args,"dev",eval=True)
    test_dataloader = DataLoader(args,"test",inference=True)

    if args.test_trivial_kb_embedding:
        entity_embs = np.load(os.path.join(args.emb_dir,"trivial_entity_embedding.npy"))
        relation_embs = np.load(os.path.join(args.emb_dir,"trivial_relation_embedding.npy"))
    else:
        entity_embs = np.load(os.path.join(args.emb_dir,"entity_embedding.npy"))
        relation_embs = np.load(os.path.join(args.emb_dir,"relation_embedding.npy"))
    logger.info("embeddings loaded.")
    logger.info(f"start training...seed:{args.seed}")
    
    # training     
    torch.cuda.empty_cache()
    set_seed(args)
    model = BertForSequenceClassification.from_pretrained('microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract',config=config,
						          task_name=args.task_name,
                                                          kb_embs=entity_embs,
                                                          rel_embs=relation_embs,
                                                          num_labels=args.num_labels,
                                                          mode=args.mode)
    model.to(args.device)

    checkpoint, _, _, _ = train(args,train_dataloader,dev_dataloader,model)
       
    model = BertForSequenceClassification.from_pretrained(checkpoint,config=config,
                                                          task_name=args.task_name,
                                                          kb_embs=entity_embs,
                                                          rel_embs=relation_embs,
                                                          num_labels=args.num_labels,
                                                          mode=args.mode)
    model.to(args.device)
  
    dev_preds = evaluate(dev_dataloader,model,args.num_labels,eval=True) 
    test_preds = evaluate(test_dataloader,model,args.num_labels,predict_only=True)
    

    with open(os.path.join(output_dir,"dev_preds.npy"), "wb") as fp:
        np.save(fp,dev_preds)
    with open(os.path.join(output_dir,"test_preds.npy"),"wb") as fp:
        np.save(fp,test_preds)

    print("finished.")
    
if __name__ == "__main__":
    main()
