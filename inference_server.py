from flask import Flask, request, Response, json
from flask_cors import CORS
import torch
import threading
from transformers import pipeline, AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer
from peft import PeftModel
import uuid
import random

app = Flask(__name__)
CORS(app)

# Global models dictionary
MODELS = {
    "LISTENER": None,
    "RESPONDER": None,
    "EMOTION": None,
    "SENTIMENT": None
}

def load_models():
    print("Loading AI models into memory (GPU)...")
    
    # 1. Listener
    MODELS["LISTENER"] = pipeline("text-classification", model="kartik2705/zenture-listener")
    
    # 2. Responder (Llama-3.2-3B + LoRA)
    base_model_id = "unsloth/llama-3.2-3b-instruct-bnb-4bit"
    adapter_id = "kartik2705/ZentureResponder"
    
    print(f"Loading Base: {base_model_id}")
    base_model = AutoModelForCausalLM.from_pretrained(
        base_model_id,
        device_map={"": 0},
        trust_remote_code=True
    )
    
    print(f"Loading Adapter: {adapter_id}")
    model = PeftModel.from_pretrained(base_model, adapter_id)
    tokenizer = AutoTokenizer.from_pretrained(adapter_id)
    MODELS["RESPONDER"] = {"model": model, "tokenizer": tokenizer}
    
    # 3. Analytics
    MODELS["EMOTION"] = pipeline("text-classification", model="bhadresh-savani/bert-base-uncased-emotion", return_all_scores=True)
    MODELS["SENTIMENT"] = pipeline("text-classification", model="distilbert-base-uncased-finetuned-sst-2-english")
    
    print("All models loaded successfully.")

@app.route('/generate', methods=['POST'])
def generate():
    data = request.get_json()
    user_input = data.get('prompt', '')
    sys_prompt = data.get('sys_prompt', '')
    history_prompt = data.get('history_prompt', '')
    
    # 1. Classification (Listener)
    analysis = MODELS["LISTENER"](user_input)[0]
    predicted_label = analysis['label']
    confidence_score = analysis['score']
    
    # 2. Analytics
    raw_emotions = MODELS["EMOTION"](user_input)
    emotions = raw_emotions[0] if isinstance(raw_emotions[0], list) else raw_emotions
    emotion_label = max(emotions, key=lambda x: x['score'])['label']
    
    sentiment_res = MODELS["SENTIMENT"](user_input)[0]
    sentiment_score = 1.0 if sentiment_res['label'] == 'POSITIVE' else 0.0

    # 3. Assemble Full Prompt
    formatted_prompt = f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{sys_prompt}<|eot_id|>{history_prompt}<|start_header_id|>user<|end_header_id|>\n\n{user_input}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"

    # 4. Streamed Generation
    responder = MODELS["RESPONDER"]
    model = responder["model"]
    tokenizer = responder["tokenizer"]
    
    streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    
    generation_kwargs = dict(
        input_ids=tokenizer(formatted_prompt, return_tensors="pt").input_ids.to(model.device),
        streamer=streamer,
        max_new_tokens=512,
        do_sample=True,
        temperature=0.4,
        top_p=0.9,
        repetition_penalty=1.3,
        eos_token_id=tokenizer.eos_token_id,
    )

    thread = threading.Thread(target=model.generate, kwargs=generation_kwargs)
    thread.start()

    def stream_response():
        full_text = ""
        for new_text in streamer:
            full_text += new_text
            yield f"data: {json.dumps({'chunk': new_text})}\n\n"
        
        # Meta info at the end
        yield f"data: {json.dumps({
            'final': True, 
            'full_response': full_text,
            'predicted_label': predicted_label,
            'confidence_score': confidence_score,
            'emotion_label': emotion_label,
            'sentiment_score': sentiment_score
        })}\n\n"

    return Response(stream_response(), mimetype='text/event-stream')

if __name__ == '__main__':
    load_models()
    app.run(host='0.0.0.0', port=5001, threaded=True)
