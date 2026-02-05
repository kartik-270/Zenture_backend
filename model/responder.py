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
    'validation': list(chain(dataset_esconv['validation'], dataset_augesc['validation']))
}

# Step 2: Prepare a Causal Language Modeling Dataset
# We need to format the dialogues into a continuous text stream
def format_dialogue(dialogue):
    text = ""
    for turn in dialogue['dialogue']:
        role = turn['speaker']
        content = turn['content']
        text += f"<{role}> {content} "
    return {"text": text}

combined_dataset_formatted = {
    'train': [format_dialogue(d) for d in combined_dataset['train']],
    'validation': [format_dialogue(d) for d in combined_dataset['validation']]
}

# Step 3: Load Tokenizer and Model
model_name = "microsoft/DialoGPT-medium"
tokenizer = AutoTokenizer.from_pretrained(model_name)
tokenizer.pad_token = tokenizer.eos_token # GPT-2 and DialoGPT models need a padding token
model = AutoModelForCausalLM.from_pretrained(model_name)

def tokenize_function(examples):
    return tokenizer(examples['text'], truncation=True, padding="max_length", max_length=512)

tokenized_datasets = {
    'train': tokenizer(list(d['text'] for d in combined_dataset_formatted['train']), truncation=True, padding="max_length", max_length=512),
    'validation': tokenizer(list(d['text'] for d in combined_dataset_formatted['validation']), truncation=True, padding="max_length", max_length=512)
}

# Step 4: Define Training Arguments and Train
training_args = TrainingArguments(
    output_dir="./results_responder",
    overwrite_output_dir=True,
    num_train_epochs=3,
    per_device_train_batch_size=2,
    per_device_eval_batch_size=2,
    warmup_steps=500,
    weight_decay=0.01,
    evaluation_strategy="epoch",
    logging_dir='./logs_responder',
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=tokenized_datasets['train'],
    eval_dataset=tokenized_datasets['validation'],
)

# trainer.train()

# Save the fine-tuned model
# trainer.save_model("./responder_model")