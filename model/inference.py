# Assuming both models are trained and saved
from transformers import pipeline

# Load the trained listener model using the pipeline
# Ensure paths are correct relative to the 'sih' directory
listener_pipe = pipeline("text-classification", model="./listener_model", tokenizer="./listener_model")

# Load the trained responder model (LoRA)
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer
import torch

base_model_name = "microsoft/DialoGPT-medium"
print("Loading base model...")
base_model = AutoModelForCausalLM.from_pretrained(base_model_name)
print("Loading LoRA adapters...")
model = PeftModel.from_pretrained(base_model, "./responder_model")
tokenizer = AutoTokenizer.from_pretrained("./responder_model")

responder_pipe = pipeline("text-generation", model=model, tokenizer=tokenizer)

def chatbot_response(user_input):
    # Module 1: Analyze user input
    analysis_result = listener_pipe(user_input)[0]
    condition = analysis_result['label']
    
    # Module 2: Generate response based on analysis
    # This is a simplified example. You would create a more complex prompt.
    prompt = f"The user is feeling {condition}. User: {user_input} Bot: "
    
    response = responder_pipe(prompt, max_length=100, do_sample=True, top_k=50, top_p=0.95, pad_token_id=responder_pipe.tokenizer.eos_token_id)
    return response[0]['generated_text']

# Example usage
user_message = "I've been feeling really sad lately."
response = chatbot_response(user_message)
print(response)