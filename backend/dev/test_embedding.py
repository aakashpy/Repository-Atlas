from sentence_transformers import SentenceTransformer

model = SentenceTransformer("all-MiniLM-L6-v2", device="cuda") 
print("Model loaded on:", model.device) 
sentence = "This is a test of our RAG embedding pipeline." 
embedding = model.encode(sentence) 
print("Embedding shape:", embedding.shape) 
print("First 5 values:", embedding[:5])