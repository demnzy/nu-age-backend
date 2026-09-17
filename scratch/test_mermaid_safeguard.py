import re
import base64

def clean_mermaid_syntax(mermaid_code: str) -> str:
    clean = mermaid_code.strip()
    clean = re.sub(r"^```(?:mermaid)?\s*", "", clean)
    clean = re.sub(r"\s*```$", "", clean)
    clean = clean.replace("\\n", "\n")

    # 1. Header Separation: Ensure header keyword is on its own line
    clean = re.sub(
        r'^(flowchart\s+[A-Za-z]+|graph\s+[A-Za-z]+|sequenceDiagram|stateDiagram-v2|classDiagram|erDiagram|mindmap)\s+',
        r'\1\n  ',
        clean,
        flags=re.IGNORECASE
    )

    # 2. Statement Splitting: If statements were smashed together on a single line, split them
    # Matches: [Node] NextNode --> or {Node} NextNode -->
    clean = re.sub(
        r'([\]\}\)])\s+([A-Za-z0-9_]+)(?=\s*(?:-->|-.->|==>|--\s*[^->\n]+\s*-->))',
        r'\1\n  \2',
        clean
    )

    # 3. Arrow text normalization: Convert "-- Label -->" into "-->|Label|" for rock-solid Mermaid compatibility
    clean = re.sub(r'--\s*([^->\n]+?)\s*-->', r'-->|\1|', clean)

    # 4. Truncated / dangling node repair: If an arrow points to a dangling typo or unbracketed token at end
    clean = re.sub(r'-->\s*([A-Za-z0-9_]+)\s*$', r'--> \1["\1"]', clean)

    return clean.strip()


sample_1 = "flowchart LR A[Fetch Request] --> B{Cache Available?} B -- Yes --> C[Return Cached Response] B -- No --> D[Fetch from Network] D --> E[Cache Response] E --> Cng"
sample_2 = "flowchart LR A[Fetch Request] --> B{Cache Available?} B -- Yes --> C[Return Cached Response] B -- No --> D[Fetch from Network] D --> E[Cache Response] E --> F[Notify Tabs of Update]"

print("--- Cleaned Sample 1 ---")
c1 = clean_mermaid_syntax(sample_1)
print(c1)

print("\n--- Cleaned Sample 2 ---")
c2 = clean_mermaid_syntax(sample_2)
print(c2)

b64_1 = base64.urlsafe_b64encode(c1.encode("utf-8")).decode("utf-8")
url_1 = f"https://mermaid.ink/img/{b64_1}?bgColor=FFFFFF"
print("\nGenerated URL 1:\n", url_1)
