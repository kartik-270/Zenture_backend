import gradio as gr
import torch
from transformers import pipeline, AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel
import os

# --- CONFIGURATION ---
# TODO: REPLACE 'YOUR_USERNAME' WITH YOUR ACTUAL HUGGING FACE USERNAME
# Example: HF_USERNAME = "kartik270"
HF_USERNAME = os.environ.get("HF_USERNAME", "kartik2705")

LISTENER_REPO = f"{HF_USERNAME}/zenture-listener"
RESPONDER_REPO = f"{HF_USERNAME}/zenture-responder-lora"

print(f"Loading models from: {LISTENER_REPO} and {RESPONDER_REPO}")

# --- LOAD MODELS ---
try:
    # 1. Listener (Sentiment/Intent)
    listener = pipeline("text-classification", model=LISTENER_REPO)

    # 2. Responder (DialoGPT + LoRA)
    base_model = AutoModelForCausalLM.from_pretrained("microsoft/DialoGPT-medium")
    responder_model = PeftModel.from_pretrained(base_model, RESPONDER_REPO)
    tokenizer = AutoTokenizer.from_pretrained("microsoft/DialoGPT-medium")
    
    # Merge for inference if needed, or just use as is
    responder_model.eval()

except Exception as e:
    print(f"Error loading models: {e}")
    print("Did you upload the models and update HF_USERNAME?")
    raise e

# --- INFERENCE LOGIC ---
CRISIS_RESPONSE = "I'm so sorry you're going through this. Please know that help is available. You can connect with someone immediately by calling 988 or finding a local crisis hotline."

def predict(message, history):
    """
    Main inference function for Gradio.
    message: Current user message
    history: List of [user_msg, bot_msg] from previous turns
    """
    if not message:
        return "Please say something."

    # 1. Listen (Analysis)
    try:
        analysis = listener(message)[0]
        label = analysis['label']
        score = analysis['score']
        print(f"Input: {message} | Label: {label} | Score: {score}")
    except Exception as e:
        print(f"Listener Error: {e}")
        label = "neutral"
        score = 0.0

    # 2. Crisis Check
    if label == "Suicidal" and score > 0.8:
        return CRISIS_RESPONSE

    # 3. Construct Prompt with History
    # DialoGPT expects a continuous string or ids. Here we fine-tuned on <speaker> format.
    # But standard DialoGPT inference usually just takes the chat history ids.
    # Since we used a custom format in training (implied from responder.py), we should try to match it.
    
    # Flatten history for context
    prompt_context = ""
    # Limit context to last 3 turns to fit in context window
    recent_history = history[-6:] # 3 turns = 6 messages (user + assistant)
    
    # In newer Gradio, history is a list of dicts: {'role': 'user'/'assistant', 'content': '...'}
    for msg in recent_history:
        role_tag = "<usr>" if msg.get("role") == "user" else "<sys>"
        content = msg.get("content", "")
        if content:
            prompt_context += f"{role_tag} {content} "
            
    full_prompt = f"{prompt_context}<usr> {message} <sys>"
    
    # 4. Generate (Respond)
    inputs = tokenizer(full_prompt, return_tensors="pt")
    
    # Move to GPU if available in the Space
    if torch.cuda.is_available():
        inputs = inputs.to("cuda")
        responder_model.to("cuda")

    with torch.no_grad():
        output = responder_model.generate(
            **inputs, 
            max_new_tokens=50,
            do_sample=True,
            top_p=0.9,
            top_k=50,
            temperature=0.7,
            repetition_penalty=1.2, # Prevent loops like <> <> <>
            no_repeat_ngram_size=3, # Prevent repeating phrases
            pad_token_id=tokenizer.eos_token_id,
            eos_token_id=tokenizer.eos_token_id
        )
    
    generated_text = tokenizer.decode(output[0], skip_special_tokens=True)
    
    # Post-processing to extract just the new response
    # The generated text contains the full prompt plus the new generated tokens.
    # We split by <sys> and take the LAST part to get just the chatbot's response.
    
    parts = generated_text.split("<sys>")
    if len(parts) > 1:
        response = parts[-1].strip()
        # It might generate the next user turn like "<usr> ...". We must cut it off.
        response = response.split("<usr>")[0].strip()
        # DialoGPT sometimes hallucinates speakers like ":" or "usr:" or "sys:".
        # Let's clean some common ones up if they start the sentence.
        for prefix in ["usr:", "sys:", ":", "usr", "sys"]:
            if response.startswith(prefix):
                response = response[len(prefix):].strip()
    else:
        # Fallback if split fails
        response = generated_text[len(full_prompt):].strip()

    # If it still ends up empty after cleaning
    if not response:
        response = "I hear you. Could you tell me more about what's going on?"

    return response

# --- LAUNCH APP ---
demo = gr.ChatInterface(
    fn=predict, 
    title="Zenture Wellness AI",
    description="Mental Health Support Chatbot (Powered by DialoGPT + LoRA)"
)

if __name__ == "__main__":
    demo.launch()
