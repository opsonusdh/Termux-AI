import os
import sys
from pathlib import Path

# Adjust the Python path to import from core
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "core")))
import memory_store

def run_indexer(path: str, extension_filter: str | None = None):
    target_path = Path(path)
    if not target_path.exists():
        print(f"Error: Path does not exist: {path}")
        return

    if extension_filter:
        extensions = {f".{ext.strip().lstrip('.')}" for ext in extension_filter.split(',')}
    else:
        extensions = set() # Index all text files if no filter

    indexed_files_count = 0
    indexed_chunks_count = 0

    files_to_index = []
    if target_path.is_file():
        files_to_index.append(target_path)
    elif target_path.is_dir():
        for root, _, files in os.walk(target_path):
            for file in files:
                p = Path(root) / file
                if (not extensions and p.suffix.lower() in [".txt", ".md", ".py", ".sh", ".json", ".yaml", ".yml", ".xml", ".html", ".css", ".js", ".java", ".c", ".cpp", ".h", ".hpp"]) or (p.suffix.lower() in extensions):
                    files_to_index.append(p)
    
    for file_path in files_to_index:
        try:
            content = file_path.read_text(encoding='utf-8')
            if content.strip(): # Only index if there's actual content
                print(f"Indexing file: {file_path}")
                chunks = memory_store.chunk_text(content)
                if chunks:
                    memory_store.index_memory(content, source_path=str(file_path))
                    indexed_chunks_count += len(chunks)
                    indexed_files_count += 1
                else:
                    print(f"No meaningful chunks found for {file_path}")
            else:
                print(f"Skipping empty file: {file_path}")
        except Exception as e:
            print(f"Error processing file {file_path}: {e}")

    print(f"\nIndexing complete. Indexed {indexed_chunks_count} chunks from {indexed_files_count} files.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Index files into dedicated memory store.")
    parser.add_argument("path", type=str, help="Path to the directory or file to index.")
    parser.add_argument("--extension_filter", type=str, default=None, help="Comma-separated extensions (e.g., '.py,.md'). If empty, indexes common text files.")
    args = parser.parse_args()

    run_indexer(args.path, args.extension_filter)
