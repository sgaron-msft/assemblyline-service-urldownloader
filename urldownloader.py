import base64
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime
from urllib.parse import urlparse

import requests
import yaml
from assemblyline.common.identify import Identify
from assemblyline.odm.base import DATEFORMAT
from assemblyline.odm.models.ontology.results.http import HTTP as HTTPResult
from assemblyline.odm.models.ontology.results.network import NetworkConnection
from assemblyline.odm.models.ontology.results.sandbox import Sandbox
from assemblyline_service_utilities.common.tag_helper import add_tag
from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.request import ServiceRequest
from assemblyline_v4_service.common.result import (
    Result,
    ResultImageSection,
    ResultKeyValueSection,
    ResultOrderedKeyValueSection,
    ResultTableSection,
    ResultTextSection,
    TableRow,
)
from assemblyline_v4_service.common.task import PARENT_RELATION
from bs4 import BeautifulSoup
from PIL import UnidentifiedImageError
from requests.exceptions import ConnectionError, TooManyRedirects

KANGOOROO_FOLDER = os.path.join(os.path.dirname(__file__), "kangooroo")

# Regex from
# https://stackoverflow.com/questions/40939380/how-to-get-file-name-from-content-disposition
# Many tests can be found at http://test.greenbytes.de/tech/tc2231/
UTF8_FILENAME_REGEX = r"filename\*=UTF-8''([\w%\-\.]+)(?:; ?|$)"
ASCII_FILENAME_REGEX = r"filename=([\"']?)(.*?[^\\])\1(?:; ?|$)"


def detect_open_directory(request: ServiceRequest, soup: BeautifulSoup):
    if not soup.title or "index of" not in soup.title.string.lower():
        return

    open_directory_links = []
    open_directory_folders = []
    for a in soup.find_all("a", href=True):
        if "://" in a["href"][:10] and a["href"][0] != ".":
            continue
        if a["href"][0] == "?":
            # Probably just some table ordering
            continue
        if a["href"][0] == "/":
            # Check if it is the parent directory
            if a["href"] in request.task.fileinfo.uri_info.uri:
                continue

        if a["href"].endswith("/"):
            open_directory_folders.append(a["href"])
        else:
            open_directory_links.append(a["href"])

    if open_directory_links or open_directory_folders:
        open_directory_section = ResultTextSection("Open Directory Detected", parent=request.result)
        if open_directory_links:
            open_directory_section.add_line(f"File{'s' if len(open_directory_links) > 1 else ''}:")

        for link in open_directory_links:
            # Append the full website, remove the '.' from the link
            link = f"{request.task.fileinfo.uri_info.uri.rstrip('/')}/{link.lstrip('./')}"
            open_directory_section.add_line(link)
            add_tag(open_directory_section, "network.static.uri", link)

        if open_directory_folders:
            open_directory_section.add_line(f"Folder{'s' if len(open_directory_folders) > 1 else ''}:")

        for link in open_directory_folders:
            # Append the full website, remove the '.' from the link
            link = f"{request.task.fileinfo.uri_info.uri.rstrip('/')}/{link.lstrip('./')}"
            open_directory_section.add_line(link)
            add_tag(open_directory_section, "network.static.uri", link)


class URLDownloader(ServiceBase):
    def __init__(self, config=None) -> None:
        super().__init__(config)
        self.identify = Identify(use_cache=False)
        self.request_timeout = self.config.get("request_timeout", 150)
        self.do_not_download_regexes = [re.compile(x) for x in self.config.get("do_not_download_regexes", [])]
        self.no_sandbox = self.config.get("no_sandbox", False)
        with open(os.path.join(KANGOOROO_FOLDER, "default_conf.yml"), "r") as f:
            self.default_kangooroo_config = yaml.safe_load(f)

    def execute(self, request: ServiceRequest) -> None:
        result = Result()
        request.result = result

        with open(request.file_path, "r") as f:
            data = yaml.safe_load(f)

        data.pop("uri")
        for no_dl in self.do_not_download_regexes:
            # Do nothing if we are not supposed to scan that URL
            if no_dl.match(request.task.fileinfo.uri_info.uri):
                return

        method = data.pop("method", "GET")
        if method == "GET":
            if "\x00" in request.task.fileinfo.uri_info.uri:
                # We won't try to fetch URIs with a null byte using subprocess.
                # This would cause a fork_exec issue. We will return an empty result instead.
                return
            headers = data.pop("headers", {})
            if data or headers:
                ignored_params_section = ResultKeyValueSection("Ignored params", parent=request.result)
                ignored_params_section.update_items(data)
                ignored_params_section.update_items(headers)

            kangooroo_config = self.default_kangooroo_config.copy()
            kangooroo_config["temporary_folder"] = os.path.join(self.working_directory, "tmp")
            os.makedirs(kangooroo_config["temporary_folder"], exist_ok=True)
            kangooroo_config["output_folder"] = os.path.join(self.working_directory, "output")
            os.makedirs(kangooroo_config["output_folder"], exist_ok=True)

            if self.config["proxies"][request.get_param("proxy")]:
                proxy = self.config["proxies"][request.get_param("proxy")]
                if isinstance(proxy, dict):
                    proxy = proxy[request.task.fileinfo.uri_info.scheme]
                url_proxy = urlparse(proxy)
                if not url_proxy.netloc:
                    # If the proxy was written as
                    # "127.0.0.1:8080"
                    # "user@127.0.0.1:8080"
                    # "user:password@127.0.0.1:8080"
                    url_proxy = urlparse(f"http://{proxy}")
                kangooroo_config["kang-upstream-proxy"]["ip"] = url_proxy.hostname
                kangooroo_config["kang-upstream-proxy"]["port"] = url_proxy.port
                if url_proxy.username:
                    kangooroo_config["kang-upstream-proxy"]["username"] = url_proxy.username
                if url_proxy.password:
                    kangooroo_config["kang-upstream-proxy"]["password"] = url_proxy.password
            else:
                kangooroo_config.pop("kang-upstream-proxy", None)

            with tempfile.NamedTemporaryFile(dir=self.working_directory, delete=False, mode="w") as temp_conf:
                yaml.dump(kangooroo_config, temp_conf)

            kangooroo_args = [
                "java",
                f"-Xmx{math.floor(self.service_attributes.docker_config.ram_mb*0.75)}m",
                "-Dlogback.configurationFile=./logback.xml",
                "-jar",
                "KangoorooStandalone.jar",
                "--conf-file",
                temp_conf.name,
                "--dont-use-captcha",  # Don't use captcha for the moment, to be enabled later
                "--url",
                request.task.fileinfo.uri_info.uri,
            ]
            if self.no_sandbox:
                kangooroo_args.insert(-2, "--no-sandbox")
            try:
                subprocess.run(kangooroo_args, cwd=KANGOOROO_FOLDER, capture_output=True, timeout=self.request_timeout)
            except subprocess.TimeoutExpired:
                timeout_section = ResultTextSection("Request timed out", parent=request.result)
                timeout_section.add_line(
                    f"Timeout of {self.request_timeout} seconds was not enough to process the query fully."
                )
                return

            url_md5 = hashlib.md5(request.task.fileinfo.uri_info.uri.encode()).hexdigest()

            output_folder = os.path.join(kangooroo_config["output_folder"], url_md5)

            if not os.path.exists(output_folder):
                # There was a mismatch between what Kangooroo fetched and the URL we requested.
                possible_folders = os.listdir(kangooroo_config["output_folder"])
                if len(possible_folders) == 0:
                    raise Exception(
                        (
                            "No Kangooroo output folder found. Kangooroo may have been OOMKilled. "
                            "Check for memory usage and increase limit as needed."
                        )
                    )
                elif len(possible_folders) != 1:
                    raise Exception(
                        (
                            "Multiple Kangooroo output folders found. Unknown situation happened, you can try "
                            "submitting this URL again to see if it would help."
                        )
                    )
                else:
                    url_hash_mismatch = ResultTextSection("URL hash mismatch", parent=request.result)
                    url_hash_mismatch.add_line(
                        (
                            f"URL '{request.task.fileinfo.uri_info.uri}' ({url_md5}) was requested "
                            f"but a different URL was fetched ({possible_folders[0]})."
                        )
                    )
                    output_folder = os.path.join(kangooroo_config["output_folder"], possible_folders[0])

            results_filepath = os.path.join(output_folder, "results.json")
            if not os.path.exists(results_filepath):
                raise Exception(
                    (
                        "No Kangooroo results.json found. Kangooroo may have been OOMKilled. "
                        "Check for memory usage and increase limit as needed."
                    )
                )
            with open(results_filepath, "r") as f:
                results = json.load(f)

            sandbox_details = {
                "analysis_metadata": {
                    "start_time": datetime.strptime(results["creationDate"], "%b %d, %Y, %I:%M:%S %p").strftime(
                        DATEFORMAT
                    )
                },
                "sandbox_name": results["engineName"],
                "sandbox_version": results["engineVersion"],
            }
            http_result = {
                "response_code": results["response_code"],
            }

            # Main result section
            target_urls = [results["requested_url"]]
            result_section = ResultOrderedKeyValueSection("Results", parent=request.result)
            result_section.add_item("response_code", results["response_code"])
            result_section.add_item("requested_url", results["requested_url"])
            add_tag(result_section, "network.static.uri", results["requested_url"])
            if "requested_url_ip" in results:
                result_section.add_item("requested_url_ip", results["requested_url_ip"])
                result_section.add_tag("network.static.ip", results["requested_url_ip"])
            if "actual_url" in results:
                target_urls.append(results["actual_url"])
                result_section.add_item("actual_url", results["actual_url"])
                add_tag(result_section, "network.static.uri", results["actual_url"])
            if "actual_url_ip" in results:
                result_section.add_item("actual_url_ip", results["actual_url_ip"])
                result_section.add_tag("network.static.ip", results["actual_url_ip"])

            if (
                "requested_url_ip" in results
                and "actual_url_ip" in results
                and results["requested_url_ip"] != results["actual_url_ip"]
            ):
                result_section.add_tag("file.behavior", "IP Redirection change")

            if (
                "requested_url" in results
                and "actual_url" in results
                and results["requested_url"] != results["actual_url"]
            ):
                http_result["redirection_url"] = results["actual_url"]

            if results.get("experimentation", {}).get("params", {}).get("window_size", False):
                sandbox_details["analysis_metadata"]["window_size"] = results["experimentation"]["params"][
                    "window_size"
                ]

            # Screenshot section
            screenshot_path = os.path.join(output_folder, "screenshot.png")
            if os.path.exists(screenshot_path):
                screenshot_section = ResultImageSection(
                    request, title_text="Screenshot of visited page", parent=request.result
                )
                screenshot_section.add_image(
                    path=screenshot_path,
                    name="screenshot.png",
                    description=f"Screenshot of {request.task.fileinfo.uri_info.uri}",
                )
                screenshot_section.promote_as_screenshot()

            # favicon section
            favicon_path = os.path.join(output_folder, "favicon.ico")
            if os.path.exists(favicon_path):
                try:
                    screenshot_section = ResultImageSection(request, title_text="Favicon of visited page")
                    screenshot_section.add_image(
                        path=favicon_path,
                        name="favicon.ico",
                        description=f"Favicon of {request.task.fileinfo.uri_info.uri}",
                    )
                    request.result.add_section(screenshot_section)
                    fileinfo = self.identify.fileinfo(favicon_path, skip_fuzzy_hashes=True, calculate_entropy=False)
                    http_result["favicon"] = {
                        "md5": fileinfo["md5"],
                        "sha1": fileinfo["sha1"],
                        "sha256": fileinfo["sha256"],
                        "size": fileinfo["size"],
                    }
                except UnidentifiedImageError:
                    # Kangooroo is sometime giving html page as favicon...
                    pass

            source_path = os.path.join(output_folder, "source.html")
            if os.path.exists(source_path):
                with open(source_path, "rb") as f:
                    data = f.read()

                soup = BeautifulSoup(data, features="lxml")
                if soup.title and soup.title.string:
                    http_result["title"] = soup.title.string

            # Find any downloaded file
            with open(os.path.join(output_folder, "session.har"), "r") as f:
                har_content = json.load(f)

            downloads = {}
            redirects = []
            response_errors = []
            for entry in har_content["log"]["entries"]:
                # Convert Kangooroo's list of header to a proper dictionary
                request_headers = {header["name"]: header["value"] for header in entry["request"]["headers"]}
                response_headers = {header["name"]: header["value"] for header in entry["response"]["headers"]}

                http_details = {
                    "request_uri": entry["request"]["url"],
                    "request_headers": request_headers,
                    "request_method": entry["request"]["method"],
                    "response_headers": response_headers,
                    "response_status_code": entry["response"]["status"],
                }

                # Figure out if there is an http redirect
                if entry["response"]["status"] in [301, 302, 303, 307, 308]:
                    redirects.append(
                        {
                            "status": entry["response"]["status"],
                            "redirecting_url": entry["request"]["url"],
                            "redirecting_ip": (
                                entry["serverIPAddress"] if "serverIPAddress" in entry else "Not Available"
                            ),
                            "redirecting_to": (
                                entry["response"]["redirectURL"]
                                if "redirectURL" in entry["response"]
                                else "Not Available"
                            ),
                        }
                    )

                # Some redirects and hidden in the headers with 200 response codes
                if "refresh" in response_headers:
                    try:
                        refresh = response_headers["refresh"].split(";", 1)
                        if int(refresh[0]) <= 15 and refresh[1].startswith("url="):
                            redirects.append(
                                {
                                    "status": entry["response"]["status"],
                                    "redirecting_url": entry["request"]["url"],
                                    "redirecting_ip": (
                                        entry["serverIPAddress"] if "serverIPAddress" in entry else "Not Available"
                                    ),
                                    "redirecting_to": refresh[1][4:],
                                }
                            )

                    except Exception:
                        # Maybe log that we weren't able to parse the refresh
                        pass

                # Find all content that was downloaded from the servers
                if "size" in entry["response"]["content"] and entry["response"]["content"]["size"] != 0:
                    content_text = entry["response"]["content"].pop("text")
                    if (
                        "encoding" in entry["response"]["content"]
                        and entry["response"]["content"]["encoding"] == "base64"
                    ):
                        try:
                            content = base64.b64decode(content_text)
                        except Exception:
                            content = content_text.encode()
                    else:
                        content = content_text.encode()
                    with tempfile.NamedTemporaryFile(
                        dir=self.working_directory, delete=False, mode="wb"
                    ) as content_file:
                        content_file.write(content)
                    fileinfo = self.identify.fileinfo(
                        content_file.name, skip_fuzzy_hashes=True, calculate_entropy=False
                    )
                    content_md5 = fileinfo["md5"]
                    entry["response"]["content"]["_replaced"] = fileinfo["sha256"]
                    http_details["response_content_fileinfo"] = {
                        "md5": fileinfo["md5"],
                        "sha1": fileinfo["sha1"],
                        "sha256": fileinfo["sha256"],
                        "size": fileinfo["size"],
                    }
                    if "mimeType" in entry["response"]["content"] and entry["response"]["content"]["mimeType"]:
                        http_details["response_content_mimetype"] = entry["response"]["content"]["mimeType"]

                    if content_md5 not in downloads:
                        downloads[content_md5] = {"path": content_file.name}

                    # The headers could contain the name of the downloaded file
                    if (
                        "Content-Disposition" in response_headers
                        # Some servers are returning an empty "Content-Disposition"
                        and response_headers["Content-Disposition"]
                    ):
                        downloads[content_md5]["filename"] = response_headers["Content-Disposition"]
                        match = re.search(ASCII_FILENAME_REGEX, downloads[content_md5]["filename"])
                        if match:
                            downloads[content_md5]["filename"] = match.group(2)

                        match = re.search(UTF8_FILENAME_REGEX, downloads[content_md5]["filename"])
                        if match:
                            downloads[content_md5]["filename"] = match.group(1)
                    else:
                        filename = None
                        requested_url = urlparse(entry["request"]["url"])
                        if "." in os.path.basename(requested_url.path):
                            filename = os.path.basename(requested_url.path)

                        if not filename:
                            possible_filename = entry["request"]["url"]
                            if len(possible_filename) > 150:
                                parsed_url = requested_url._replace(fragment="")
                                possible_filename = parsed_url.geturl()

                            if len(possible_filename) > 150:
                                parsed_url = parsed_url._replace(params="")
                                possible_filename = parsed_url.geturl()

                            if len(possible_filename) > 150:
                                parsed_url = parsed_url._replace(query="")
                                possible_filename = parsed_url.geturl()

                            if len(possible_filename) > 150:
                                parsed_url = parsed_url._replace(path="")
                                possible_filename = parsed_url.geturl()
                            filename = possible_filename

                        downloads[content_md5]["filename"] = filename

                    if not downloads[content_md5]["filename"]:
                        downloads[content_md5]["filename"] = f"UnknownFilename_{fileinfo['sha256'][:8]}"
                    downloads[content_md5]["size"] = entry["response"]["content"]["size"]
                    downloads[content_md5]["url"] = entry["request"]["url"]
                    downloads[content_md5]["mimeType"] = entry["response"]["content"]["mimeType"]
                    downloads[content_md5]["fileinfo"] = fileinfo

                if "_errorMessage" in entry["response"]:
                    response_errors.append((entry["request"]["url"], entry["response"]["_errorMessage"]))

                self.ontology.add_result_part(
                    model=NetworkConnection, data={"http_details": http_details, "connection_type": "http"}
                )

            # Add the modified entries log
            modified_har_filepath = os.path.join(self.working_directory, "modified_session.har")
            with open(modified_har_filepath, "w") as f:
                json.dump(har_content, f)
            request.add_supplementary(modified_har_filepath, "session.har", "Complete session log")

            if redirects:
                http_result["redirects"] = []
                redirect_section = ResultTableSection("Redirections", parent=request.result)
                for redirect in redirects:
                    redirect_section.add_row(TableRow(redirect))
                    add_tag(redirect_section, "network.static.uri", redirect["redirecting_url"])
                    redirect_section.add_tag("network.static.ip", redirect["redirecting_ip"])
                    add_tag(redirect_section, "network.static.uri", redirect["redirecting_to"])
                    http_result["redirects"].append(
                        {"from_url": redirect["redirecting_url"], "to_url": redirect["redirecting_to"]}
                    )
                redirect_section.set_column_order(["status", "redirecting_url", "redirecting_ip", "redirecting_to"])

            self.ontology.add_result_part(model=Sandbox, data=sandbox_details)
            self.ontology.add_result_part(model=HTTPResult, data=http_result)

            if downloads:
                content_section = ResultTableSection("Downloaded Content")
                safelisted_section = ResultTableSection("Safelisted Content")
                for download_params in downloads.values():
                    file_info = download_params["fileinfo"]
                    added = True

                    if (
                        download_params["url"] in target_urls
                        or len(downloads) == 1
                        or re.match(request.get_param("regex_extract_filetype"), file_info["type"])
                        or (
                            request.get_param("extract_unmatched_filetype")
                            and not re.match(request.get_param("regex_supplementary_filetype"), file_info["type"])
                        )
                    ):
                        if download_params["url"] in target_urls:
                            try:
                                with open(download_params["path"], "rb") as f:
                                    data = f.read()
                                soup = BeautifulSoup(data, features="lxml")
                                detect_open_directory(request, soup)
                            except Exception:
                                pass

                        added = request.add_extracted(
                            download_params["path"],
                            download_params["filename"],
                            download_params["url"] or "Unknown URL",
                            safelist_interface=self.api_interface,
                            parent_relation=PARENT_RELATION.DOWNLOADED,
                        )
                    else:
                        request.add_supplementary(
                            download_params["path"],
                            download_params["filename"],
                            download_params["url"] or "Unknown URL",
                            parent_relation=PARENT_RELATION.DOWNLOADED,
                        )

                    (content_section if added else safelisted_section).add_row(
                        TableRow(
                            dict(
                                Filename=download_params["filename"],
                                Size=download_params["size"],
                                mimeType=download_params["mimeType"],
                                url=download_params["url"],
                                SHA256=file_info["sha256"],
                            )
                        )
                    )

                if content_section.body:
                    request.result.add_section(content_section)
                if safelisted_section.body:
                    request.result.add_section(safelisted_section)

            if response_errors:
                error_section = ResultTextSection("Responses Error", parent=request.result)
                for response_url, response_error in response_errors:
                    error_section.add_line(f"{response_url}: {response_error}")
        else:
            # Non-GET request
            try:
                r = requests.request(
                    method,
                    request.task.fileinfo.uri_info.uri,
                    headers=data.get("headers", {}),
                    proxies=self.config["proxies"][request.get_param("proxy")],
                    data=data.get("data", None),
                    json=data.get("json", None),
                )
            except ConnectionError:
                error_section = ResultTextSection("Error", parent=request.result)
                error_section.add_line(f"Cannot connect to {request.task.fileinfo.uri_info.hostname}")
                error_section.add_line("This server is currently unavailable")
                return
            except TooManyRedirects as e:
                error_section = ResultTextSection("Too many redirects", parent=request.result)
                error_section.add_line(f"Cannot connect to {request.task.fileinfo.uri_info.hostname}")

                redirect_section = ResultTableSection("Redirections", parent=error_section)
                for redirect in e.response.history:
                    redirect_section.add_row(
                        TableRow({"status": redirect.status_code, "redirecting_url": redirect.url})
                    )
                    add_tag(redirect_section, "network.static.uri", redirect.url)
                redirect_section.set_column_order(["status", "redirecting_url"])
                return
            requests_content_path = os.path.join(self.working_directory, "requests_content")
            with open(requests_content_path, "wb") as f:
                f.write(r.content)
            file_info = self.identify.fileinfo(requests_content_path, skip_fuzzy_hashes=True, calculate_entropy=False)
            if file_info["type"].startswith("archive"):
                request.add_extracted(
                    requests_content_path,
                    file_info["sha256"],
                    "Archive from the URI",
                    parent_relation=PARENT_RELATION.DOWNLOADED,
                )
            else:
                request.add_supplementary(requests_content_path, file_info["sha256"], "Full content from the URI")
