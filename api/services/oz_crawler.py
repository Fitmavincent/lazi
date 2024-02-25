from bs4 import BeautifulSoup
from datetime import datetime
import requests
import re

base_url = "https://www.ozbargain.com.au"
default_wish_list = ['LEGO', 'Xiaomi', 'DJI', 'iPhone', 'Apple', 'RTX']

headers = {
    'user-agent':
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36'
}

class OzCrawler:

    def oz_crawl_pipeline(self, page: int, wish_list: list = []):

        data = []
        urls = [base_url]

        if (wish_list is None) or (len(wish_list) == 0):
            wish_list = default_wish_list

        # url construction
        for i in range(2, page + 1):
            urls.append(f'{base_url}/?page={i}')

        # data fetching
        for url in urls:
            print(f'Fetching data from {url}...')
            page_data = self.get_page_data(url)
            data.extend(page_data)

        # data filtering
        # if item name contains any of the wish list item and item tag is not expired, then add it to the filtered data
        filtered_data = [item for item in data if any(wish_item in item['name'] for wish_item in wish_list) and item['tag'] != 'expired']

        return filtered_data

    def get_page_data(self, url: str):

        page = requests.get(url, headers=headers)
        soup = BeautifulSoup(page.content, 'html.parser')
        data = []

        ITEM_CLASS = '.node-ozbdeal'
        for item in soup.select(ITEM_CLASS):
            expired_tag = item.select_one('.tagger.expired').text if item.select_one('.tagger.expired') else None
            upcoming_tag = item.select_one('.tagger.upcoming').text if item.select_one('.tagger.upcoming') else None
            targeted_tag = item.select_one('.tagger.targeted').text if item.select_one('.tagger.targeted') else None
            long_running_tag = item.select_one('.tagger.longrunning').text if item.select_one('.tagger.longrunning') else None

            item_name = item.select_one('h2').get('data-title')
            item_price = item.select_one('em').text if item.select_one('em') else None
            item_link = item.select_one('a').get('href')
            item_image = item.select_one('.foxshot-container a img').get('src')
            item_time = item.select_one('div.submitted').text

            data.append({
                'name': item_name,
                'price': item_price,
                'link': f'{base_url}{item_link}',
                'node_url': self.process_node(item_link),
                'image': item_image,
                'time': self.format_time(item_time),
                'tag': self.process_tag(targeted_tag, long_running_tag, expired_tag, upcoming_tag), # 'targeted', 'long_running', 'expired', 'upcoming
            })

        return data

    def format_time(self, timeStr):
        # Regular expression to match date and time in the format 'dd/mm/yyyy - hh:mm'
        match = re.search(r'\d{2}/\d{2}/\d{4} - \d{2}:\d{2}', timeStr)
        # If match is found, convert it to a datetime object
        if match:
            datetime_str = match.group()
            datetime_object = datetime.strptime(datetime_str, '%d/%m/%Y - %H:%M')
            return datetime_object
        else:
            return None

    def process_tag(self, *tags):
        for tag in tags:
            if(tag is not None):
                return tag

    def process_node(self, item_url):
        node_url = item_url.replace('goto', 'node')
        return f'{base_url}{node_url}'