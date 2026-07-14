# NihongoViewer

An offline screen-translation tool for Japanese games, inspired by
[bquenin/interpreter](https://github.com/bquenin/interpreter).

**Status:** UI scaffold only. This currently opens a native desktop window that
renders the HTML interface. No capture / OCR / translation features are wired up yet.

## Tech stack

- **Python 3.14**
- **[pywebview](https://pywebview.flowrl.com/)** — renders the HTML/CSS/JS UI in a
  native window (uses the built-in WebView2 runtime on Windows).
- UI is plain **HTML + CSS + JS** in `ui/`.

## Project layout

```
main.py            # entry point: opens the window and loads ui/index.html
requirements.txt   # Python dependencies
run.bat            # convenience launcher (uses the .venv)
ui/
  index.html       # the interface
  style.css
  app.js           # UI-only interactions
```

## Run it

Double-click `run.bat`, or from a terminal:

```powershell
# First time only: create the environment
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# Every time
.venv\Scripts\python.exe main.py
```
