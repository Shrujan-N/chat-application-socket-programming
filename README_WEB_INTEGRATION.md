# Web UI integration

Replace `App/main.py`, `App/database.py`, and `App/static/index.html` with the files in this package.

Run from the `App` directory:
```powershell
python -m pip install -r ..\requirements.txt
python -m uvicorn main:app --reload
```

This is a local/demo implementation. The user selector is not authentication. Before public deployment, implement authenticated sessions, permissions tied to the authenticated identity, rate limits, file size/type validation, and production configuration.
