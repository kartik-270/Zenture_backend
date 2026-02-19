import torch
from datasets import load_dataset
from transformers import AutoTokenizer, AutoModelForCausalLM, Trainer, TrainingArguments
from itertools import chain

# Step 1: Load and Combine Datasets
dataset_esconv = load_dataset("thu-coai/esconv")
dataset_augesc = load_dataset("thu-coai/augesc")
# Assuming new_mental_health_conversations_all1 has a similar structure
# dataset_mh_conv = load_dataset("CalebE/new_mental_health_conversations_all1")

# We'll use a simplified example of combining them
# In a real-world scenario, you would need more robust logic to handle different formats
combined_dataset = {
    'train': list(chain(dataset_esconv['train'], dataset_augesc['train'])),
    'validation': list(dataset_esconv['validation'])
}
# Check if dataset_augesc has a validation split before adding it
if 'validation' in dataset_augesc:
    combined_dataset['validation'].extend(dataset_augesc['validation'])

import json

# Step 2: Prepare a Causal Language Modeling Dataset
# The datasets from thu-coai often contain JSON strings in the 'text' column
def format_dialogue(example):
    try:
        text_val = example.get('text')
        if isinstance(text_val, str):
            dialogue_data = json.loads(text_val)
        else:
            dialogue_data = text_val if text_val is not None else example
            
        # Handle dictionary format (like esconv)
        if isinstance(dialogue_data, dict):
            dialog_turns = dialogue_data.get('dialog', dialogue_data.get('dialogue', []))
            formatted_text = ""
            for turn in dialog_turns:
                role = turn.get('speaker', 'unknown')
                content = turn.get('text', turn.get('content', ''))
                formatted_text += f"<{role}> {content} "
            return {"text": formatted_text}
            
        # Handle list-of-pairs format (like augesc)
        elif isinstance(dialogue_data, list):
            formatted_text = ""
            for turn in dialogue_data:
                # augesc often uses [role, content]
                if isinstance(turn, (list, tuple)) and len(turn) >= 2:
                    role, content = turn[0], turn[1]
                    formatted_text += f"<{role}> {content} "
                # handle if it's a list of dicts too
                elif isinstance(turn, dict):
                    role = turn.get('speaker', turn.get('role', 'unknown'))
                    content = turn.get('text', turn.get('content', ''))
                    formatted_text += f"<{role}> {content} "
            return {"text": formatted_text}
            
        return {"text": ""}
    except Exception as e:
        print(f"Error formatting dialogue: {e}")
        return {"text": ""}

combined_dataset_formatted = {
    'train': [format_dialogue(d) for d in combined_dataset['train']],
    'validation': [format_dialogue(d) for d in combined_dataset['validation']]
}
# Filter out empty strings
combined_dataset_formatted['train'] = [d for d in combined_dataset_formatted['train'] if d['text']]
combined_dataset_formatted['validation'] = [d for d in combined_dataset_formatted['validation'] if d['text']]

# Step 3: Load Tokenizer and Model
model_name = "microsoft/DialoGPT-medium"
tokenizer = AutoTokenizer.from_pretrained(model_name)
tokenizer.pad_token = tokenizer.eos_token 
model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=torch.float32)

# OPTIMIZATION: Reduce max_length to speed up training significantly
MAX_LENGTH = 128 

def tokenize_function(examples):
    return tokenizer(examples['text'], truncation=True, padding="max_length", max_length=MAX_LENGTH)

# Prepare properly formatted Dataset objects
from datasets import Dataset
# Use a smaller subset for debugging stability
train_data_subset = combined_dataset_formatted['train'][:10000] 
val_data_subset = combined_dataset_formatted['validation'][:1000] 

train_ds = Dataset.from_list(train_data_subset)
val_ds = Dataset.from_list(val_data_subset)

tokenized_train = train_ds.map(tokenize_function, batched=True, remove_columns=["text"])
tokenized_val = val_ds.map(tokenize_function, batched=True, remove_columns=["text"])

# Using a standard DataCollator for Causal LM is more robust than manual label masking
from transformers import DataCollatorForLanguageModeling
data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

# Step 4: Define Training Arguments and Train
training_args = TrainingArguments(
    output_dir="./results_responder",
    num_train_epochs=1,
    per_device_train_batch_size=1,   # Very small batch for absolute stability
    gradient_accumulation_steps=16,  # Effective batch size of 16
    learning_rate=5e-6,              # Very conservative learning rate
    weight_decay=0.01,
    max_grad_norm=0.5,               # Strict gradient clipping
    fp16=False,
    logging_steps=10,                # Log less frequently for cleaner output
    eval_strategy="no",
    save_strategy="steps",           # Save every few steps
    save_steps=100,                  # Save every 100 steps
    save_total_limit=2,              # Keep only the last 2 checkpoints
    warmup_steps=100,
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=tokenized_train,
    eval_dataset=tokenized_val,
    data_collator=data_collator,
)

# Resume from checkpoint if it exists in the output directory
import os
checkpoint = None
if os.path.exists(training_args.output_dir) and os.listdir(training_args.output_dir):
    checkpoint = True

trainer.train(resume_from_checkpoint=checkpoint)

# Save the fine-tuned model
trainer.save_model("./responder_model")
