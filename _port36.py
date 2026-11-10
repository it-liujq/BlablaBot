import pathlib
import re
import subprocess
import tempfile

UP = pathlib.Path(r"E:\AI\_upstream")
ROOT = pathlib.Path(r"E:\AI\aliceuid")
NEW = "BlablaBot"
BASE = "467b510"
TARGET = "1b17d58"
KW = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")


def norm(t):
    return t.replace("\r\n", "\n")


def transform(t):
    return re.sub(r"AliceUID(?!_)", NEW, t)


def target_path(rel):
    rel = rel.replace('"', "")
    return ROOT / NEW / rel[len("AliceUID/"):] if rel.startswith("AliceUID/") else ROOT / rel


names = subprocess.run(["git", "diff", "--name-status", f"{BASE}..{TARGET}"], cwd=UP, **KW).stdout.splitlines()
added, modified, deleted = [], [], []
for line in names:
    parts = line.split("\t")
    st, rel = parts[0], parts[-1]
    if st.startswith("A"):
        added.append(rel)
    elif st.startswith("M"):
        modified.append(rel)
    elif st.startswith("D"):
        deleted.append(rel)

added = [r for r in added if not r.startswith("integrations/")]
modified = [r for r in modified if not r.startswith("integrations/")]
deleted = [r for r in deleted if not r.startswith("integrations/") and r != "LICENSE"]
print("A", len(added), "M", len(modified), "D(skip-integrations/license)", len(deleted))

for rel in added:
    dst = target_path(rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = transform(norm(subprocess.run(["git", "show", f"{TARGET}:{rel}"], cwd=UP, **KW).stdout))
    dst.write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")
print("added done")

clean, conflicted = [], []
for rel in modified:
    dst = target_path(rel)
    if not dst.exists():
        text = transform(norm(subprocess.run(["git", "show", f"{TARGET}:{rel}"], cwd=UP, **KW).stdout))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")
        clean.append(rel + "(new)")
        continue
    base = transform(norm(subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=UP, **KW).stdout))
    theirs = transform(norm(subprocess.run(["git", "show", f"{TARGET}:{rel}"], cwd=UP, **KW).stdout))
    ours = norm(dst.read_text(encoding="utf-8", errors="replace"))
    if ours == base:
        dst.write_text(theirs.replace("\n", "\r\n"), encoding="utf-8", newline="")
        clean.append(rel)
        continue
    with tempfile.TemporaryDirectory() as td:
        b = pathlib.Path(td, "b"); o = pathlib.Path(td, "o"); t = pathlib.Path(td, "t")
        b.write_text(base, encoding="utf-8"); o.write_text(ours, encoding="utf-8"); t.write_text(theirs, encoding="utf-8")
        r = subprocess.run(["git", "merge-file", "-p", "--diff3", str(o), str(b), str(t)], **KW)
        merged = r.stdout
    conflicted.append((rel, "<<<<<<<" in merged))
    dst.write_text(merged.replace("\n", "\r\n"), encoding="utf-8", newline="")

print("clean:", len(clean))
print("merged:", conflicted)
for rel in deleted:
    p = target_path(rel)
    if p.exists():
        p.unlink()
        print("deleted", rel)
