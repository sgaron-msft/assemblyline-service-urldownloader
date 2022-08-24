import requests

from assemblyline.common import forge
from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.request import ServiceRequest
from assemblyline_v4_service.common.result import Result, ResultTableSection, TableRow

from tempfile import NamedTemporaryFile
from typing import Union

config = forge.get_config()
SUBMISSION_HEADERS = config.ui.url_submission_headers
SUBMISSION_PROXIES = config.ui.url_submission_proxies


class URLDownloader(ServiceBase):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.content_type_filter: list = config.get('content_type_filter', [])
        self.content_type_filter.append(None)

    def fetch_uri(self, uri: str, apply_filter: bool = True, headers={}) -> Union[str, requests.Response]:
        resp = requests.head(uri, allow_redirects=True, timeout=10, headers=headers, proxies=SUBMISSION_PROXIES)
        # Only concerned with gathering responses of interest
        if resp.ok:
            if apply_filter and any(content_type in resp.headers.get('Content-Type')
                                    for content_type in self.content_type_filter):
                return
            resp_fh = NamedTemporaryFile(delete=False)
            resp_fh.write(requests.get(uri, allow_redirects=True, headers=headers).content)
            resp_fh.close()
            return resp_fh.name
        return resp

    def execute(self, request: ServiceRequest) -> None:
        result = Result()
        submitted_url = []
        minimum_maliciousness = request.get_param('minimum_maliciousness')
        headers = SUBMISSION_HEADERS
        if request.get_param('user_agent'):
            headers['User-Agent'] = request.get_param('user_agent')

        # Code to be used when responsibility of fetching submitted_url is moved to service from UI
        # -----------------------------------------------------------------------------------------
        submitted_url = request.task.metadata.get('submitted_url')
        # Make sure this is the first URL fetched
        submitted_url = [(submitted_url, 10000)] if submitted_url and request.task.depth == 0 else []

        # Only concerned with static/dynamic URIs found by prior services
        tags = request.task.tags
        urls = tags.get('network.static.uri', []) + tags.get('network.dynamic.uri', []) + submitted_url

        request.temp_submission_data.setdefault('visited_urls', [])
        exception_table = ResultTableSection("Attempted Connection Exceptions")
        for tag_value, tag_score in sorted(urls, key=lambda x: x[1]):
            # Minimize revisiting the same URIs in the same submission
            if tag_value in request.temp_submission_data['visited_urls']:
                continue
            elif tag_score < minimum_maliciousness:
                break

            request.temp_submission_data['visited_urls'].append(tag_value)
            # Write response and attach to submission
            try:
                fp = self.fetch_uri(tag_value, apply_filter=bool(tag_score < 500), headers=headers)
                if isinstance(fp, str):
                    request.add_extracted(fp, tag_value, f"Response from {tag_value}",
                                          safelist_interface=self.api_interface)
                else:
                    exception_table.add_row(TableRow({'URI': tag_value, 'REASON': fp.reason}))
            except requests.exceptions.ConnectionError as e:
                exception_table.add_row(TableRow({'URI': tag_value, 'REASON': str(e.args[0].reason).split(':', 1)[1]}))

            if exception_table.body:
                result.add_section(exception_table)

        request.result = result
