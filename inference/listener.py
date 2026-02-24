import torch
from transformers import pipeline

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

def load_listener(repo_id):
    """
    Initializes the listener pipeline.
    """
    return pipeline(
        "text-classification",
        model=repo_id,
        device=0 if DEVICE == "cuda" else -1
    )

def analyze_emotion(listener_pipeline, text):
    """
    Analyzes the emotion of the given text.
    """
    try:
        results = listener_pipeline(text)
        return results[0]
    except Exception as e:
        print(f"Listener Error: {e}")
        return {"label": "neutral", "score": 0.0}
