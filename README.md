
A minimal single-file Flask 3.x web application.

## Project Structure

```text
.
├── app.py
├── requirements.txt
└── README.md
Prerequisites
Python 3.8 or higher installed on your system.

Setup Instructions
1. File Contents
Ensure your project contains the following files:

app.py
Python
from flask import Flask

app = Flask(__name__)

@app.route("/")
def hello():
    return "<h1>Hello from Your Name</h1>"

if __name__ == "__main__":
    app.run(debug=True)
requirements.txt
Plaintext
Flask==3.0.3
Werkzeug==3.0.3
2. Create and Activate a Virtual Environment
It is recommended to use a virtual environment to manage dependencies.

On macOS / Linux:

Bash
python3 -m venv venv
source venv/bin/activate
On Windows (Command Prompt):

DOS
python -m venv venv
venv\Scripts\activate
On Windows (PowerShell):

PowerShell
python -m venv venv
.\venv\Scripts\Activate.ps1
3. Install Dependencies
Install the pinned requirements:

Bash
pip install -r requirements.txt
4. How to Run the Application
You can start the development server using either standard Flask commands or running the script directly with Python.

Option A: Using the flask command (Recommended)
Bash
flask --app app run --debug
Option B: Running directly with Python
Bash
python app.py
5. Access the Web Page
Open your web browser and navigate to:

Plaintext
[http://127.0.0.1:5000/](http://127.0.0.1:5000/)
You should see an HTML heading with "Hello from Your Name".