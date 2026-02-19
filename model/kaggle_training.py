import torch
import json
import os
from itertools import chain
from datasets import load_dataset, Dataset
from transformers import (
    AutoTokenizer, 
    AutoModelForCausalLM, 
    Trainer, 
    TrainingArguments,
    BitsAndBytesConfig,
    DataCollatorForLanguageModeling
)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

# --- Kaggle Environment Detection ---
IS_KAGGLE = os.path.exists("/kaggle/working")
OUTPUT_DIR = "/kaggle/working/results_responder" if IS_KAGGLE else "./results_responder"
MODEL_SAVE_PATH = "/kaggle/working/responder_model" if IS_KAGGLE else "./responder_model"

# Step 1: Load Datasets
print("Loading datasets...")
dataset_esconv = load_dataset("thu-coai/esconv")
dataset_augesc = load_dataset("thu-coai/augesc")

combined_dataset = {
    'train': list(chain(dataset_esconv['train'], dataset_augesc['train'])),
    'validation': list(dataset_esconv['validation'])
}
if 'validation' in dataset_augesc:
    combined_dataset['validation'].extend(dataset_augesc['validation'])

# Step 2: Format Dialogue
def format_dialogue(example):
    try:
        text_val = example.get('text')
        if isinstance(text_val, str):
            dialogue_data = json.loads(text_val)
        else:
            dialogue_data = text_val if text_val is not None else example
            
        if isinstance(dialogue_data, dict):
            dialog_turns = dialogue_data.get('dialog', dialogue_data.get('dialogue', []))
            formatted_text = ""
            for turn in dialog_turns:
                role = turn.get('speaker', 'unknown')
                content = turn.get('text', turn.get('content', ''))
                formatted_text += f"<{role}> {content} "
            return {"text": formatted_text}
            
        elif isinstance(dialogue_data, list):
            formatted_text = ""
            for turn in dialogue_data:
                if isinstance(turn, (list, tuple)) and len(turn) >= 2:
                    role, content = turn[0], turn[1]
                    formatted_text += f"<{role}> {content} "
                elif isinstance(turn, dict):
                    role = turn.get('speaker', turn.get('role', 'unknown'))
                    content = turn.get('text', turn.get('content', ''))
                    formatted_text += f"<{role}> {content} "
            return {"text": formatted_text}
        return {"text": ""}
    except Exception as e:
        return {"text": ""}

print("Formatting data...")
train_data = [format_dialogue(d) for d in combined_dataset['train'] if format_dialogue(d)['text']]
val_data = [format_dialogue(d) for d in combined_dataset['validation'] if format_dialogue(d)['text']]

# Use a larger subset for cloud training
train_ds = Dataset.from_list(train_data[:15000]) 
val_ds = Dataset.from_list(val_data[:1500])

# Step 3: Quantization & Model Load
model_name = "microsoft/DialoGPT-medium"
tokenizer = AutoTokenizer.from_pretrained(model_name)
tokenizer.pad_token = tokenizer.eos_token 

print("Configuring QLoRA...")
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.float16,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
)

model = AutoModelForCausalLM.from_pretrained(
    model_name,
    quantization_config=bnb_config,
    device_map="auto",
    trust_remote_code=True
)

model = prepare_model_for_kbit_training(model)

# Step 4: LoRA Setup
lora_config = LoraConfig(
    r=16,
    lora_alpha=32,
    target_modules=["c_attn"], # For GPT-2/DialoGPT
    lora_dropout=0.05,
    bias="none",
    task_type="CAUSAL_LM"
)

model = get_peft_model(model, lora_config)
model.print_trainable_parameters()

# Step 5: Tokenization
MAX_LENGTH = 128
def tokenize_function(examples):
    return tokenizer(examples['text'], truncation=True, padding="max_length", max_length=MAX_LENGTH)

tokenized_train = train_ds.map(tokenize_function, batched=True, remove_columns=["text"])
tokenized_val = val_ds.map(tokenize_function, batched=True, remove_columns=["text"])

data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

# Step 6: Training Arguments
training_args = TrainingArguments(
    output_dir=OUTPUT_DIR,
    num_train_epochs=1,
    per_device_train_batch_size=4,   # Increased for GPU
    gradient_accumulation_steps=4, 
    learning_rate=2e-4,              # Higher LR works better with LoRA
    weight_decay=0.01,
    max_grad_norm=0.3,
    fp16=True,                       # Use mixed precision
    logging_steps=50,
    eval_strategy="steps",
    eval_steps=200,
    save_strategy="steps",
    save_steps=200,
    save_total_limit=2,
    warmup_steps=100,
    report_to="none"                 # Set to "wandb" if you have an account
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=tokenized_train,
    eval_dataset=tokenized_val,
    data_collator=data_collator,
)

print("Starting training on Cloud/GPU...")
trainer.train()

# Step 7: Save model
print(f"Saving model to {MODEL_SAVE_PATH}...")
trainer.save_model(MODEL_SAVE_PATH)
tokenizer.save_pretrained(MODEL_SAVE_PATH)

print("Done! If on Kaggle, remember to download the output folder.")
