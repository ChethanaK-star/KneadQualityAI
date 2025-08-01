from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from uuid import uuid4
from typing import List, Union

import os
import json
import requests
import re
import ast

from utils.embed_store import embed_and_store, query_vector_store
from utils.extract_text import extract_text
load_dotenv()
# stores chat history as json 
HISTORY_DIR = "history_logs"
os.makedirs(HISTORY_DIR, exist_ok=True)  # Ensure directory exists

def get_history_file(user_id):
    return os.path.join(HISTORY_DIR, f"{user_id}.json")

def load_history(user_id):
    filepath = get_history_file(user_id)
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            return json.load(f)
    return []

def save_to_history(user_id, role, message):
    filepath = get_history_file(user_id)
    history = load_history(user_id)
    history.append({"role": role, "message": message})
    with open(filepath, "w") as f:
        json.dump(history, f, indent=2)


# Pydantic model for JSON requests
class QueryRequest(BaseModel):
    query: str
    file_name: str = None

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "documents")
PROMPT_DIR = os.getenv("PROMPT_DIR", "prompts")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PROMPT_DIR, exist_ok=True)

def process_and_store(file_path, file_name):
    text = extract_text(file_path)
    embed_and_store(text, file_name)

@app.post("/chat/")
async def chat(request: Request):
    data = await request.json()
    user_id = data.get("user_id") or str(uuid4())  # If user_id not passed, generate new

    query = data["query"]

    # Save the user message
    save_to_history(user_id, "user", query)

    # Get the chat history (optional: pass to LLM for context)
    chat_history = load_history(user_id)

    # Your existing logic goes here (LLM or RAG with FAISS)
    # For now, fake response:
    response = f"I received: '{query}' and have {len(chat_history)} messages in history."

    # Save bot response
    save_to_history(user_id, "bot", response)

    return {"response": response, "user_id": user_id}

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as f:
        f.write(await file.read())

    process_and_store(file_path, file.filename)
    print(f"Parsed filename list: {file.filename}")

    custom_prompt = f"You are an expert assistant for queries related to the document titled '{file.filename}'. Answer with clear and concise explanations based only on the given context."
    with open(os.path.join(PROMPT_DIR, f"{file.filename}.json"), "w") as f:
        json.dump({"system_prompt": custom_prompt}, f)

    return {"message": f"{file.filename} uploaded, processed, and prompt saved.", "filename": file.filename}

from typing import List, Union
from fastapi import Form

@app.post("/query")
async def query_document(
    query: str = Form(...),
    file_name: Union[List[str], str, None] = Form(None),
    user_id: str = Form(None)
):
    if not query or not query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    print(f">> Received query: '{query}' for files: '{file_name}'")

    # Ensure file_name is a list
    if isinstance(file_name, str):
        file_name = [file_name]
    elif file_name is None:
        file_name = []

    context_parts = []

    for fname in file_name:
        try:
            ctx = query_vector_store(query, fname)
            context_parts.append(f"From {fname}:\n{ctx}")
        except Exception as e:
            print(f">> Error querying vector store for {fname}: {e}")

    context = "\n\n".join(context_parts) if context_parts else "No specific document context available."
    print(">>> Final context sent to LLM:\n", context)

    # Determine prompt file (use first one if multiple)
    prompt_file = file_name[0] if file_name else None
    prompt_path = os.path.join(PROMPT_DIR, f"{prompt_file}.json") if prompt_file else None

    if prompt_path and os.path.exists(prompt_path):
        with open(prompt_path, "r") as f:
            system_prompt = json.load(f)["system_prompt"]
    else:
        system_prompt = "You are a helpful assistant specializing in quality management and process improvement."

    user_prompt = f"""Use the following context to answer clearly.

Context:
{context}

Question:
{query}

Answer:"""

    suggestion_prompt = f"Based on the topic and user's interest, give 4 short follow-up questions max of 2 or 3 words each related to: {query}. Only return them as a plain list of text questions without numbering."

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            return {"error": "Missing API key."}

        payload = {
            "model": "mistralai/mistral-7b-instruct",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "max_tokens": 300,
            "temperature": 0.7
        }

        response_main = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json=payload
        )

        suggestion_response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "mistralai/mistral-7b-instruct",
                "messages": [
                    {"role": "user", "content": suggestion_prompt}
                ],
                "max_tokens": 50,
                "temperature": 0.7
            }
        )

        ai_reply = ""
        if response_main.status_code == 200:
            data = response_main.json()
            ai_reply = data["choices"][0]["message"]["content"]
            if user_id:
                save_to_history(user_id, "user", query)
                save_to_history(user_id, "bot", ai_reply)
        else:
            return {"error": "Main AI request failed", "body": response_main.text}

        suggestions = []
        if suggestion_response.status_code == 200:
            raw = suggestion_response.json()["choices"][0]["message"]["content"]
            suggestions = [
                re.sub(r'^[\"\“\‘\']?\d*\.\s*(.+?)[\"\”\’\']?$', r'\1', line.strip())[1:]
                for line in raw.strip().splitlines()
                if line.strip()
            ]

            ai_reply = re.sub(r'(?<!")(\d+)\.\s+', r'\n\1. ', ai_reply)

        return {
            "result": ai_reply,
            "suggested_questions": suggestions
        }

    except Exception as e:
        print(f">> Exception during AI call: {e}")
        raise HTTPException(status_code=500, detail=f"AI service error: {str(e)}")

@app.post("/query-json")
async def query_document_json(request: QueryRequest):
    """Alternative JSON endpoint for queries"""
    return await query_document(request.query, request.file_name)