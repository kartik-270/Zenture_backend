import torch
from datasets import load_dataset, DatasetDict
from transformers import AutoTokenizer, AutoModelForSequenceClassification, Trainer, TrainingArguments
import numpy as np
from sklearn.metrics import accuracy_score, precision_recall_fscore_support

# Step 1: Load and Prepare the Dataset
# The dataset you're using is a CSV file. The columns are 'Unnamed: 0', 'statement', and 'status'.
# The 'status' column is the one you need for the labels.
# The 'statement' column is the text you need for the input.
dataset = load_dataset("btwitssayan/sentiment-analysis-for-mental-health")

# Rename the columns to match what the rest of the code expects
# This makes the code more readable and consistent with common practices.
dataset = dataset.rename_columns({"statement": "text", "status": "label"})

# We'll use a portion for validation
train_test_split = dataset['train'].train_test_split(test_size=0.2)
dataset = DatasetDict({
    'train': train_test_split['train'],
    'validation': train_test_split['test']
})

# Map string labels to integers
labels = dataset['train'].unique('label')
label_to_id = {label: i for i, label in enumerate(labels)}
id_to_label = {i: label for i, label in enumerate(labels)}
num_labels = len(labels)

def preprocess_function(examples):
    # This line is now correct because we renamed the column to 'label'
    examples["label"] = [label_to_id[label] for label in examples["label"]]
    return examples

dataset = dataset.map(preprocess_function, batched=True)

# Step 2: Load Tokenizer and Model
model_name = "distilbert-base-uncased"
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForSequenceClassification.from_pretrained(model_name, num_labels=num_labels)
model.config.id2label = id_to_label
model.config.label2id = label_to_id

def tokenize_function(examples):
    # This line is now correct because we renamed the column to 'text'
    return tokenizer(examples["text"], padding="max_length", truncation=True, max_length=128)

tokenized_datasets = dataset.map(tokenize_function, batched=True)

# Step 3: Define Metrics and Training Arguments
def compute_metrics(eval_pred):
    logits, labels = eval_pred
    predictions = np.argmax(logits, axis=-1)
    precision, recall, f1, _ = precision_recall_fscore_support(labels, predictions, average='weighted', zero_division=0)
    acc = accuracy_score(labels, predictions)
    return {
        'accuracy': acc,
        'f1': f1,
        'precision': precision,
        'recall': recall
    }

training_args = TrainingArguments(
    output_dir="./results_listener",
    num_train_epochs=3,
    per_device_train_batch_size=8,
    per_device_eval_batch_size=8,
    warmup_steps=500,
    weight_decay=0.01,
    eval_strategy="epoch",  
    logging_steps=10,
)

# Step 4: Train the Model
trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=tokenized_datasets["train"],
    eval_dataset=tokenized_datasets["validation"],
    processing_class=tokenizer,
    compute_metrics=compute_metrics,
)

trainer.train()

# Save the fine-tuned model
trainer.save_model("./listener_model")