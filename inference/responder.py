import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel
from .prompts import SYSTEM_PROMPT

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

def load_responder(base_model_path, lora_repo_id):
    """
    Loads the responder model and tokenizer.
    """
    base_model = AutoModelForCausalLM.from_pretrained(
        base_model_path,
        torch_dtype=torch.float16 if DEVICE == "cuda" else torch.float32
    )
    responder_model = PeftModel.from_pretrained(base_model, lora_repo_id)
    responder_model.to(DEVICE)
    responder_model.eval()
    
    tokenizer = AutoTokenizer.from_pretrained(base_model_path)
    # Ensure padding token is set
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
        
    return responder_model, tokenizer

def format_history(history):
    """
    Formats history for Llama 3.2 Instruct.
    """
    prompt_context = ""
    # Limit context
    recent_history = history[-6:]
    
    for msg in recent_history:
        if isinstance(msg, dict):
            role = "user" if msg.get("role") == "user" else "assistant"
            content = msg.get("content", "")
            prompt_context += f"<|start_header_id|>{role}<|end_header_id|>\n\n{content}<|eot_id|>"
        elif isinstance(msg, (list, tuple)):
            user_content, bot_content = msg
            if user_content:
                prompt_context += f"<|start_header_id|>user<|end_header_id|>\n\n{user_content}<|eot_id|>"
            if bot_content:
                prompt_context += f"<|start_header_id|>assistant<|end_header_id|>\n\n{bot_content}<|eot_id|>"
                
    return prompt_context

import re

def generate_response(model, tokenizer, message, history):
    """
    Generates a response using Llama 3.2 Instruct template.
    """
    prompt_context = format_history(history)
    
    # Llama 3 Instruct Format
    full_prompt = (
        f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{SYSTEM_PROMPT}<|eot_id|>"
        f"{prompt_context}<|start_header_id|>user<|end_header_id|>\n\n{message}<|eot_id|>"
        f"<|start_header_id|>assistant<|end_header_id|>\n\n"
    )
    
    inputs = tokenizer(full_prompt, return_tensors="pt").to(DEVICE)
    
    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=300, # Increased for "long para" support
            temperature=0.7,
            top_p=0.9,
            top_k=50,
            repetition_penalty=1.1,
            no_repeat_ngram_size=3,
            do_sample=True,
            pad_token_id=tokenizer.eos_token_id,
            eos_token_id=tokenizer.eos_token_id
        )
    
    generated_text = tokenizer.decode(output[0], skip_special_tokens=False)
    
    # Robust extraction: Get everything after the prompt
    # Llama 3 doesn't skip special tokens in decode easily if we want to catch <|eot_id|>
    response_raw = generated_text[generated_text.find("<|start_header_id|>assistant<|end_header_id|>\n\n") + len("<|start_header_id|>assistant<|end_header_id|>\n\n") :].strip()
    
    # Clean up trailing special tokens
    clean_response = response_raw.replace("<|eot_id|>", "").replace("<|begin_of_text|>", "").strip()
    
    # Split if it starts generating next user turn
    clean_response = clean_response.split("<|start_header_id|>")[0].strip()

    if not clean_response or len(clean_response) < 2:
        clean_response = "I'm here for you. Could you tell me more about how you're feeling today?"
        
    return clean_response
