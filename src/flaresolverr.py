import json
import logging
import os
import sys
import asyncio
import requests

import certifi
from bottle import run, response, Bottle, request, ServerAdapter

from bottle_plugins.error_plugin import error_plugin
from bottle_plugins.logger_plugin import logger_plugin
from bottle_plugins import prometheus_plugin
from dtos import V1RequestBase
import flaresolverr_service
import flaresolverr_service_nd
import utils


class JSONErrorBottle(Bottle):
    """
    Handle 404 errors
    """
    def default_error_handler(self, res):
        response.content_type = 'application/json'
        return json.dumps(dict(error=res.body, status_code=res.status_code))


app = JSONErrorBottle()


@app.route('/')
def index():
    """
    Show welcome message
    """
    res = flaresolverr_service.index_endpoint()
    return utils.object_to_dict(res)


@app.route('/health')
def health():
    """
    Healthcheck endpoint.
    This endpoint is special because it doesn't print traces
    """
    res = flaresolverr_service.health_endpoint()
    return utils.object_to_dict(res)


@app.post('/v1')
def controller_v1():
    """
    Controller v1
    """
    req = V1RequestBase(request.json)
    if utils.DRIVER_SELECTION == "nodriver":
        res = asyncio.run(flaresolverr_service_nd.controller_v1_endpoint_nd(req))
    else:
        res = flaresolverr_service.controller_v1_endpoint(req)
    if res.__error_500__:
        response.status = 500
    return utils.object_to_dict(res)

@app.get('/content')
def content():
    target_url = request.query.get('url')
    
    if not target_url:
        response.status = 400
        return {"error": "Missing 'url' in query parameters."}

    # Get the User-Agent and Cookie from the incoming request headers
    user_agent = request.headers.get('User-Agent', 'Unknown')
    cookie = request.headers.get('Cookie', '')

    # Forward these headers in the new request
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookie
    }

    try:
        # Make the request to the target URL
        external_response = requests.get(target_url, headers=headers)

        # Set the response content type to JSON
        response.content_type = 'application/json'

        # Return the response from the external API
        return {
            "message": "Request forwarded successfully",
            "target_url": target_url,
            "received_headers": {
                "User-Agent": user_agent,
                "Cookie": cookie
            },
            "status": external_response.status_code,
            "external_api_response": external_response.text
        }
    except requests.exceptions.RequestException as e:
        response.status = 500
        return {"error": str(e)}


if __name__ == "__main__":
    # check python version
    if sys.version_info < (3, 9):
        raise Exception("The Python version is less than 3.9, a version equal to or higher is required.")

    # fix for HEADLESS=false in Windows binary
    # https://stackoverflow.com/a/27694505
    if os.name == 'nt':
        import multiprocessing
        multiprocessing.freeze_support()

    # fix ssl certificates for compiled binaries
    # https://github.com/pyinstaller/pyinstaller/issues/7229
    # https://stackoverflow.com/questions/55736855/how-to-change-the-cafile-argument-in-the-ssl-module-in-python3
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    os.environ["SSL_CERT_FILE"] = certifi.where()

    # validate configuration
    log_level = os.environ.get('LOG_LEVEL', 'info').upper()
    log_html = utils.get_config_log_html()
    headless = utils.get_config_headless()
    server_host = os.environ.get('HOST', '0.0.0.0')
    server_port = int(os.environ.get('PORT', 8191))

    # check if undetected-chromedriver or nodriver is selected
    utils.get_driver_selection()

    # configure logger
    logger_format = '%(asctime)s %(levelname)-8s %(message)s'
    if log_level == 'DEBUG':
        logger_format = '%(asctime)s %(levelname)-8s ReqId %(thread)s %(message)s'
    logging.basicConfig(
        format=logger_format,
        level=log_level,
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    # disable warning traces from urllib3
    logging.getLogger('urllib3').setLevel(logging.ERROR)
    logging.getLogger('selenium.webdriver.remote.remote_connection').setLevel(logging.WARNING)
    logging.getLogger('undetected_chromedriver').setLevel(logging.WARNING)
    # nodriver is very verbose in debug
    logging.getLogger('nd.core.element').disabled = True
    logging.getLogger('nodriver.core.browser').disabled = True
    logging.getLogger('nodriver.core.tab').disabled = True
    logging.getLogger('websockets.client').disabled = True

    logging.info(f'FlareSolverr {utils.get_flaresolverr_version()}')
    logging.debug('Debug log enabled')
    logging.info("WARNING: YOU ARE RUNNING AN UNOFFICIAL EXPERIMENTAL BRANCH OF FLARESOLVER WHICH MAY CONTAIN BUGS.")
    logging.info("WARNING: IF YOU ENCOUNTER ANY, PLEASE REPORT THEM ON GITHUB AT THE FOLLOWING LINK:")
    logging.info("WARNING: https://github.com/FlareSolverr/FlareSolverr/pull/1163")

    # Get current OS for global variable
    utils.get_current_platform()

    # test browser installation for undetected-chromedriver or start loop for nodriver
    if utils.DRIVER_SELECTION == "nodriver":
        asyncio.run(flaresolverr_service_nd.test_browser_installation_nd())
    else:
        flaresolverr_service.test_browser_installation_uc()

    # start bootle plugins
    # plugin order is important
    app.install(logger_plugin)
    app.install(error_plugin)
    prometheus_plugin.setup()
    app.install(prometheus_plugin.prometheus_plugin)

    # start webserver
    # default server 'wsgiref' does not support concurrent requests
    # https://github.com/FlareSolverr/FlareSolverr/issues/680
    # https://github.com/Pylons/waitress/issues/31
    class WaitressServerPoll(ServerAdapter):
        def run(self, handler):
            from waitress import serve
            serve(handler, host=self.host, port=self.port, asyncore_use_poll=True)
    run(app, host=server_host, port=server_port, quiet=True, server=WaitressServerPoll)