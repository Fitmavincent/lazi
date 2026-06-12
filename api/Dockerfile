FROM python:3.12-bookworm

WORKDIR /src/api

COPY ./requirements.txt /src/api/requirements.txt

RUN pip install --no-cache-dir --upgrade setuptools -r /src/api/requirements.txt
# Firefox for the Woolies/V1 Playwright crawlers; Chromium (+ OS deps) for
# the Scrapling 0.4 StealthyFetcher used by the Coles V2.5 crawler.
RUN playwright install --with-deps firefox
RUN scrapling install

COPY . /src/api

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
