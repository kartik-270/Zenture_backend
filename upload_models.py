import os
from huggingface_hub import HfApi, login

def upload_models():
    print("=== Zenture Wellness Model Uploader ===")
    print("This script will upload your local models to Hugging Face.")
    
    # 1. Login
    token = os.environ.get('HF_TOKEN')
    if not token:
        print("\nPlease enter your Hugging Face Write Token.")
        print("You can get one at: https://huggingface.co/settings/tokens")
        token = input("Token: ").strip()
    
    if not token:
        print("Token is required.")
        return

    login(token=token)
    api = HfApi()
    
    try:
        user_info = api.whoami()
        username = user_info['name']
        print(f"\nLogged in as: {username}")
    except Exception as e:
        print(f"Login failed: {e}")
        return

    # 2. Upload Listener
    listener_repo = f"{username}/zenture-listener"
    print(f"\n[1/2] Processing Listener Model...")
    print(f"Target Repo: {listener_repo}")
    
    try:
        api.create_repo(repo_id=listener_repo, exist_ok=True)
        print("Uploading files (this may take a while)...")
        api.upload_folder(
            folder_path="./listener_model",
            repo_id=listener_repo,
            repo_type="model"
        )
        print("Listener Model uploaded successfully!")
    except Exception as e:
        print(f"Failed to upload Listener: {e}")

    # 3. Upload Responder
    responder_repo = f"{username}/zenture-responder-lora"
    print(f"\n[2/2] Processing Responder Model (LoRA)...")
    print(f"Target Repo: {responder_repo}")
    
    try:
        api.create_repo(repo_id=responder_repo, exist_ok=True)
        print("Uploading files (this may take a while)...")
        api.upload_folder(
            folder_path="./responder_model",
            repo_id=responder_repo,
            repo_type="model"
        )
        print("Responder Model uploaded successfully!")
    except Exception as e:
        print(f"Failed to upload Responder: {e}")

    print("\nXXX IMPORTANT XXX")
    print(f"Your models are uploaded.")
    print(f"Please update 'space_app.py' with your username: {username}")

if __name__ == "__main__":
    upload_models()
