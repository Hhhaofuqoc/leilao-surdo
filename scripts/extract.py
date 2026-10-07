#!/usr/bin/env python3
"""Extrai o primeiro JSON valido {ouvi,lance,taunt} da saida bruta do opencode."""
import json, re, sys

def main():
    if len(sys.argv) < 3:
        print("uso: extract.py <raw> <out>", file=sys.stderr); return 2
    try:
        raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
    except OSError:
        return 1
    # remove blocos de codigo markdown
    cleaned = re.sub(r"```(?:json)?", "", raw)
    # acha todos os objetos {} e tenta parsear
    for m in re.finditer(r"\{", cleaned):
        try:
            obj, end = json.JSONDecoder().raw_decode(cleaned[m.start():])
        except Exception:
            continue
        if isinstance(obj, dict) and "ouvi" in obj and "lance" in obj:
            try:
                obj["lance"] = int(obj["lance"])
            except Exception:
                obj["lance"] = 10
            obj.setdefault("taunt", "confia")
            obj.setdefault("confianca", "media")
            with open(sys.argv[2], "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            return 0
    return 1

if __name__ == "__main__":
    sys.exit(main())
