# 🌌 Zenture Wellness Backend (SIH)

The backend infrastructure for Zenture Wellness, a comprehensive mental health support platform for students. This repository contains the Flask API services and the AI Inference Engine.

## 🏗️ Backend Architecture

The backend follows a decoupled architecture consisting of two primary components:

### 1. **Flask API (Core Service)**
Acts as the main application gateway, handling business logic, database management, and user authentication.
- **Framework:** Flask
- **ORM:** SQLAlchemy (PostgreSQL)
- **Authentication:** JWT (JSON Web Tokens)
- **Storage:** Cloudinary for media/image uploads
- **Email:** SMTP (Gmail) + Maileroo API

### 2. **AI Inference Server**
A dedicated service for high-performance AI model serving, optimized for RTX 3050 GPUs.
- **Base Model:** Llama-3.2-3B (Instruct version)
- **Quantization:** 4-bit NF4 (via BitsAndBytes) for VRAM efficiency (~3GB usage)
- **Customization:** Fine-tuned via LoRA (ZentureResponder adapter)
- **Inference Stack:** Transformers, PEFT, PyTorch
- **Features:** 
    - **Intent Classification:** Identifies stress, anxiety, crisis, etc.
    - **Empathetic Response Generation:** 6-part counseling-style responses.
    - **Facial Emotion Analysis:** ViT-based stress detection via images.
    - **Sentiment Tracking:** Real-time sentiment analysis of conversations.

---

## 📂 Project Structure

```bash
sih/
├── app.py                  # Main Flask application entry point
├── config.py               # Environment & system configuration
├── models.py               # Database schemas (SQLAlchemy)
├── routes.py               # Main API endpoints (auth, user, etc.)
├── community_routes.py     # Community-specific logic
├── inference_server.py     # AI Inference Engine (Llama-3 + Analysis)
├── extensions.py           # Flask extensions (DB, JWT, Mail init)
├── migrations/             # Database migration history
├── instance/               # Local database instance (for SQLite dev)
└── inference/              # Safety filters and inference utilities
```

---

## 🚀 Getting Started

### Prerequisites
- Python 3.9+
- CUDA-enabled GPU (Optional, required for full AI inference)
- PostgreSQL (Production DB)

### Setup
1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd sih
   ```

2. **Create a Virtual Environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   ```

3. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Variables:**
   Copy `.env.example` to `.env` and fill in your credentials.
   ```bash
   cp .env.example .env
   ```

5. **Initialize Database:**
   ```bash
   python init_db.py
   ```

---

## 🛠️ Running the Backend

### Start the AI Inference Server (Port 5001)
*Run this first if you need chatbot functionality.*
```bash
python inference_server.py
```

### Start the Flask API (Port 5000)
```bash
python app.py
```

---

## 📈 Monitoring & Health
- **App Health:** Check `GET /`
- **Inference Health:** Check `GET /health` (returns GPU/VRAM status)
- **Documentation:** See `DOCUMENTATION_INDEX.md` for detailed technical logs.

---

## 📄 License
Internal SIH Project - Zenture Wellness Team
