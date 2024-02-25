# Lazi API server

## Installation

Setup your virtual environment

`python -m venv lazi-env`

Activate virtual environment

`source ./lazi-env/Scripts/activate`

Install packages

`pip install -r requirements.txt`

## Run this server

You can run this server with `uvicorn` locally:

`uvicorn main:app --reload`

Or you can run this server inside docker by using the `setup.sh`:

- To spin up the server: `./setup.sh -r`

- To stop the server: `./setup.sh -s`

- To delete the container: `./setup.sh -d`

