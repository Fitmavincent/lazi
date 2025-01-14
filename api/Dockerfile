FROM python:3.12

WORKDIR /src/api

COPY ./requirements.txt /src/api/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /src/api/requirements.txt
RUN playwright install --with-deps

COPY . /src/api

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]