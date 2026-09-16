import base64
import httpx

def render_mermaid_url(mermaid_code: str) -> str:
    clean_code = mermaid_code.strip()
    b64 = base64.urlsafe_b64encode(clean_code.encode("utf-8")).decode("utf-8")
    return f"https://mermaid.ink/img/{b64}?bgColor=FFFFFF"

diagram = """
graph LR
    A[Pointer ptr] -->|points to| B[Memory Address 0x7ffd]
    B --> C[Integer Value: 42]
"""

url = render_mermaid_url(diagram)
print("Generated URL:", url)

# Verify mermaid.ink returns valid image
with httpx.Client(timeout=10.0) as client:
    r = client.get(url)
    print("Mermaid Status:", r.status_code)
    print("Content-Type:", r.headers.get("content-type"))
    print("Bytes length:", len(r.content))
