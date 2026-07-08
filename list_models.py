"""Liệt kê các model Gemini khả dụng với API key của bạn.

Dùng:  python list_models.py
"""
import os
from dotenv import load_dotenv
from google import genai

load_dotenv()
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

for m in client.models.list():
    actions = getattr(m, "supported_actions", None) or []
    # chỉ hiện model dùng để generate content
    if "generateContent" in actions:
        print(f"{m.name:45} {getattr(m, 'display_name', '')}")
