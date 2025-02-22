import logging
import os
import pprint
import shutil
import tarfile
from base64 import urlsafe_b64encode
from collections import namedtuple

import yaml

from scanners import generic_authentication_factory
from scanners import RapidastScanner
from scanners.downloaders import authenticated_download_with_rtoken


CLASSNAME = "Zap"


pp = pprint.PrettyPrinter(indent=4)

# Helper: absolute path to this directory (which is not the current directory)
# Useful for finding files in this directory
MODULE_DIR = os.path.dirname(__file__)


class Zap(RapidastScanner):
    ## CONSTANTS
    DEFAULT_CONTEXT = "Default Context"
    AF_TEMPLATE = "af-template.yaml"
    USER = "test1"

    DEFAULT_CONTAINER = "podman"

    REPORTS_SUBDIR = "reports"

    ## FUNCTIONS
    def __init__(self, config):
        logging.debug("Initializing ZAP scanner")
        super().__init__(config)

        self.results_dir = os.path.join(
            self.config.get("config.results_dir", default="results"), "zap"
        )

        # This is used to construct the ZAP Automation config.
        # It will be saved to a file during setup phase
        # and used by the ZAP command during run phase
        self.automation_config = {}

        # When state is READY, this will contain the entire ZAP command that the container layer should run
        self.zap_cli = []

        # Defines whether a User has been created
        self.authenticated = False

        # Instanciate a PathMaps with predifined mapping IDs. They will be filled by the typed scanners
        # List important locations for host <-> container mapping point
        # + work: where data is stored:
        #   - AF file, reports, evidence, etc. are beneath this path
        # + scripts: where scripts are stored
        # + policies: where policies are stored
        self.path_map = None  # to be defined by the typed scanner

    ###############################################################
    # PUBLIC METHODS                                              #
    # Called via inheritence only                                 #
    ###############################################################

    def setup(self):
        """Prepares everything:
        - the command line to run
        - environment variables
        - files & directory

        This code handles only the "ZAP" layer, independently of the container used.
        This method should not be called directly, but only via super() from a child's setup()
        """
        logging.info("Preparing ZAP configuration")
        self._setup_zap_cli()
        self._setup_zap_automation()

    def run(self):
        """This code handles only the "ZAP" layer, independently of the container used.
        This method should not be called directly, but only via super() from a child's setup()
        This method is currently empty as running entirely depends on the containment
        """
        pass

    def postprocess(self):
        logging.info(f"Extracting report, storing in {self.results_dir}")
        reports_dir = os.path.join(self.path_map.workdir.host_path, Zap.REPORTS_SUBDIR)
        shutil.copytree(reports_dir, self.results_dir)

        logging.info("Saving the session as evidence")
        with tarfile.open(f"{self.results_dir}/session.tar.gz", "w:gz") as tar:
            tar.add(self.path_map.workdir.host_path, arcname="evidences")

    def cleanup(self):
        """Generic ZAP cleanup: should be called only via super() inheritance"""
        pass

    def data_for_defect_dojo(self):
        """Return a tuple containing:
        1) Metadata for the test (dictionary)
        2) Path to the result file (string)
        For additional info regarding the metadata, see the `import-scan`/`reimport-scan`
        endpoints (https://demo.defectdojo.org/api/v2/doc/)

        To "cancel", return the (None, None) tuple
        """
        if not self._should_export_to_defect_dojo():
            return None, None
        logging.debug("Preparing data for Defect Dojo")

        # the XML report is supposed to have been forcefully added, and expected to exist
        filename = f"{self.results_dir}/zap-report.xml"

        # default, mandatory values (which can be overloaded)
        data = {
            "scan_type": "ZAP Scan",
            "active": True,
            "verified": False,
        }

        # lists of configured import parameters
        params_root = "scanners.zap.defectDojoExport.parameters"
        import_params = self.config.get(params_root, default={}).keys()

        # overload that list onto the defaults
        for param in import_params:
            data[param] = self.config.get(f"{params_root}.{param}")

        if data.get("test") is None:
            # No test ID provided, so we need to make sure there is enough info
            # But we can't make it default (they should not be filled if there is a test ID
            if not data.get("product_name"):
                data["product_name"] = self.config.get(
                    "application.ProductName"
                ) or self.config.get("application.shortName")
            if not data.get("engagement_name"):
                data["engagement_name"] = "RapiDAST"

        return data, filename

    ###############################################################
    # PROTECTED METHODS                                           #
    # Called via Zap or inheritence only                          #
    # May be overloaded by inheriting classes                     #
    ###############################################################

    def _setup_zap_cli(self):
        """
        Complete the zap_cli list of ZAP argument.
        This is must be overloaded by descendant, which optionally call this one
        If called, the descendant must fill at least the executable
        """

        # Proxy workaround (because it currently can't be configured from Automation Framework)
        proxy = self.config.get("scanners.zap.proxy")
        if proxy:
            self.zap_cli += [
                "-config",
                f"network.connection.httpProxy.host={proxy.get('proxyHost')}",
                "-config",
                f"network.connection.httpProxy.port={proxy.get('proxyPort')}",
                "-config",
                "network.connection.httpProxy.enabled=true",
            ]
        else:
            self.zap_cli += ["-config", "network.connection.httpProxy.enabled=false"]

        # Create a session, to store them as evidence
        self.zap_cli.append("-newsession")
        self.zap_cli.append(f"{self._container_work_dir()}/session_data/session")

        if not self.config.get("scanners.zap.miscOptions.enableUI", default=False):
            # Disable UI
            self.zap_cli.append("-cmd")

        # finally: the Automation Framework:
        self.zap_cli.extend(["-autorun", f"{self._container_work_dir()}/af.yaml"])

    def get_type(self):
        """Return container type, based on configuration.
        This is only a helper to shorten long lines
        """
        return self.config.get(
            "scanners.zap.container.type", default=Zap.DEFAULT_CONTAINER
        )

    # disabling these 2 rules only here since they might actually be useful else where
    # pylint: disable=unused-argument
    def _add_env(self, key, value=None):
        logging.warning(
            "_add_env() was called on the parent ZAP class. This is likely a bug. No operation done"
        )

    def _host_work_dir(self):
        """Shortcut to the host path of the work directory"""
        return self.path_map.workdir.host_path

    def _container_work_dir(self):
        """Shortcut to the container path of the work directory"""
        return self.path_map.workdir.container_path

    def _include_file(self, host_path, dest_in_container=None):
        """Copies the file from host_path on the host to dest_in_container in the container
        Notes:
            - MUST be run after the mapping is done
            - If dest_in_container evaluates to False, default to `PathIds.WORK`
            - If dest_in_container is a directory, copy the file to it without renaming it
        """
        # 1. Compute host path
        if not dest_in_container:
            path_to_dest = self._host_work_dir()
        else:
            path_to_dest = self.path_map.container_2_host(dest_in_container)

        try:
            shutil.copy(host_path, path_to_dest)
        except shutil.SameFileError:
            logging.debug(
                f"_include_file() ignoring '{host_path} → 'container:{path_to_dest}' as they are the same file"
            )
        logging.debug(f"_include_file() '{host_path} → 'container:{path_to_dest}'")

    ###############################################################
    # PRIVATE METHODS                                             #
    # Those are called only from Zap itself                       #
    ###############################################################
    def _setup_zap_automation(self):
        # Load the Automation template
        try:
            af_template = f"{MODULE_DIR}/{Zap.AF_TEMPLATE}"
            logging.debug("Load the Automation Framework template")
            with open(af_template, "r", encoding="utf-8") as stream:
                self.automation_config = yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            raise RuntimeError(
                f"Something went wrong while parsing the config '{af_template}':\n {str(exc)}"
            ) from exc

        # Configure the basic environment target
        try:
            af_context = find_context(self.automation_config)
            af_context["urls"].append(self.config.get("application.url"))
            af_context["includePaths"].extend(
                self.config.get("scanners.zap.urls.includes", default=[])
            )
            af_context["excludePaths"].extend(
                self.config.get("scanners.zap.urls.excludes", default=[])
            )
        except KeyError as exc:
            raise RuntimeError(
                f"Something went wrong with the Zap scanner configuration, while creating the context':\n {str(exc)}"
            ) from exc

        # authentication MUST happen first in case a user is created
        self.authenticated = self.authentication_factory()

        # Create the AF configuration
        self._setup_spider()
        self._setup_ajax_spider()
        self._setup_api()
        self._setup_graphql()
        self._setup_import_urls()
        self._setup_passive_scan()
        self._setup_active_scan()
        self._setup_passive_wait()
        self._setup_report()

        # The AF should now be setup and ready to be written
        self._save_automation_file()

    def _setup_import_urls(self):
        """If scanners.zap.importUrlsFromFile exists: prepare an import job for URLs
        scanners.zap.importUrlsFromFile _must_ be an existing file on the host
        Its content is a text file: a list of GET URLs, each of which will be scanned
        """
        job = {"name": "import", "type": "import", "parameters": {"type": "url"}}

        orig = self.config.get("scanners.zap.importUrlsFromFile")
        if not orig:
            return
        dest = f"{self._container_work_dir()}/importUrls.txt"
        self._include_file(orig, dest)
        job["parameters"]["fileName"] = dest
        self.automation_config["jobs"].append(job)

    def _setup_api(self):
        """Prepare an openapi job and append it to the job list"""

        openapi = {"name": "openapi", "type": "openapi", "parameters": {}}
        api = self.config.get("scanners.zap.apiScan.apis", default={})
        if api.get("apiUrl"):
            openapi["parameters"]["apiUrl"] = api.get("apiUrl")
        elif api.get("apiFile"):
            # copy the file in the container's result directory
            # This allows the OpenAPI to be kept as evidence
            container_openapi_file = f"{self._container_work_dir()}/openapi.json"

            self._include_file(
                host_path=api.get("apiFile"), dest_in_container=container_openapi_file
            )
            openapi["parameters"]["apiFile"] = container_openapi_file
        else:
            logging.warning("No API defined in the config, in scanners.zap.apiScan.api")
        # default target: main URL, or can be overridden in apiScan
        openapi["parameters"]["targetUrl"] = self.config.get(
            "scanners.zap.apiScan.target", default=False
        ) or self.config.get("application.url")
        openapi["parameters"]["context"] = Zap.DEFAULT_CONTEXT

        self.automation_config["jobs"].append(openapi)

    def _setup_spider(self):
        """Prepare an spider job and append it to the job list"""

        if self.config.get("scanners.zap.spider", default=False) is False:
            return

        af_spider = {
            "name": "spider",
            "type": "spider",
            "parameters": {
                "user": Zap.USER if self.authenticated else "",
                "maxDuration": self.config.get(
                    "scanners.zap.spider.maxDuration", default=0
                ),
                "url": self.config.get("scanners.zap.spider.url", default=""),
            },
        }

        # Add to includePaths to the context
        if self.config.get("scanners.zap.spider.url"):
            new_include_path = self.config.get("scanners.zap.spider.url") + ".*"
            af_context = find_context(self.automation_config)
            af_context["includePaths"].append(new_include_path)

        self.automation_config["jobs"].append(af_spider)

    def _setup_ajax_spider(self):
        """Prepare an spiderAjax job and append it to the job list"""

        if self.config.get("scanners.zap.spiderAjax", default=False) is False:
            return

        af_spider_ajax = {
            "name": "spiderAjax",
            "type": "spiderAjax",
            "parameters": {
                "user": Zap.USER if self.authenticated else "",
                "maxDuration": self.config.get(
                    "scanners.zap.spiderAjax.maxDuration", default=0
                ),
                "url": self.config.get("scanners.zap.spiderAjax.url", default=""),
                "browserId": self.config.get(
                    "scanners.zap.spiderAjax.browserId", default="chrome-headless"
                ),
            },
        }

        # Add to includePaths to the context
        if self.config.get("scanners.zap.spiderAjax.url"):
            new_include_path = self.config.get("scanners.zap.spiderAjax.url") + ".*"
            af_context = find_context(self.automation_config)
            af_context["includePaths"].append(new_include_path)

        self.automation_config["jobs"].append(af_spider_ajax)

    def _setup_graphql(self):
        """Prepare a graphql job and append it to the job list"""

        if self.config.get("scanners.zap.graphql", default=False) is False:
            return

        af_graphql = {
            "name": "graphql",
            "type": "graphql",
            "parameters": self.config.get(
                "scanners.zap.graphql", default={"endpoint": ""}
            ),
        }

        host_file = self.config.get("scanners.zap.graphql.schemaFile")
        if host_file:
            cont_file = os.path.join(self._container_work_dir(), "schema.graphql")
            self._include_file(host_path=host_file, dest_in_container=cont_file)
            af_graphql["parameters"]["schemaFile"] = cont_file

        self.automation_config["jobs"].append(af_graphql)

    def _setup_passive_scan(self):
        """Adds the passive scan to the job list. Needs to be done prior to Active scan"""

        if self.config.get("scanners.zap.passiveScan", default=False) is False:
            return

        # passive AF schema
        passive = {
            "name": "passiveScan-config",
            "type": "passiveScan-config",
            "parameters": {
                "maxAlertsPerRule": 10,
                "scanOnlyInScope": True,
                "maxBodySizeInBytesToScan": 10000,
                "enableTags": False,
            },
            "rules": [],
        }

        # Fetch the list of disabled passive scan as scanners.zap.policy.disabledPassiveScan
        disabled = self.config.get("scanners.zap.passiveScan.disabledRules", default="")
        # ''.split('.') returns [''], which is a non-empty list (which would erroneously get into the loop later)
        disabled = disabled.split(",") if len(disabled) else []
        logging.debug(f"disabling the following passive scans: {disabled}")
        for rulenum in disabled:
            passive["rules"].append({"id": int(rulenum), "threshold": "off"})

        self.automation_config["jobs"].append(passive)

    def _setup_passive_wait(self):
        """Adds a wait to the list of jobs, to make sure that the Passive Scan is finished"""

        if self.config.get("scanners.zap.passiveScan", default=False) is False:
            return

        # Available Parameters: maximum time to wait
        waitfor = {
            "type": "passiveScan-wait",
            "name": "passiveScan-wait",
            "parameters": {},
        }
        self.automation_config["jobs"].append(waitfor)

    def _setup_active_scan(self):
        """Adds the active scan job list."""

        if self.config.get("scanners.zap.activeScan", default=False) is False:
            return

        active = {
            "name": "activeScan",
            "type": "activeScan",
            "parameters": {
                "context": Zap.DEFAULT_CONTEXT,
                "user": Zap.USER if self.authenticated else "",
                "policy": self.config.get(
                    "scanners.zap.activeScan.policy", default="API-scan-minimal"
                ),
            },
        }

        self.automation_config["jobs"].append(active)

    def _construct_report_af(self, report_format):
        report_af = {
            "name": "report",
            "type": "report",
            "parameters": {
                "template": report_format.template,
                "reportDir": f"{self.path_map.workdir.container_path}/{Zap.REPORTS_SUBDIR}/",
                "reportFile": report_format.name,
                "reportTitle": "ZAP Scanning Report",
                "reportDescription": "",
                "displayReport": False,
            },
        }

        return report_af

    def _should_export_to_defect_dojo(self):
        """Return a truthful value if Defect Dojo export is configured and not disbaled"""
        return (
            self.config.exists("scanners.zap.defectDojoExport")
            and self.config.get("scanners.zap.defectDojoExport.type") is not False
        )

    def _setup_report(self):
        """Adds the report to the job list. This should be called last"""

        os.makedirs(os.path.join(self.path_map.workdir.host_path, Zap.REPORTS_SUBDIR))
        ReportFormat = namedtuple("ReportFormat", ["template", "name"])
        reports = {
            "json": ReportFormat("traditional-json-plus", "zap-report.json"),
            "html": ReportFormat("traditional-html-plus", "zap-report.html"),
            "sarif": ReportFormat("sarif-json", "zap-report.sarif.json"),
            "xml": ReportFormat("traditional-xml-plus", "zap-report.xml"),
        }

        formats = set(self.config.get("scanners.zap.report.format", {"json"}))
        # DefectDojo requires XML report type
        if self._should_export_to_defect_dojo():
            logging.debug("ZAP report: ensures XML report for Defect Dojo")
            formats.add("xml")

        appended = 0
        for format_id in formats:
            try:
                logging.debug(
                    f"report {format_id}, filename: {reports[format_id].name}"
                )
                self.automation_config["jobs"].append(
                    self._construct_report_af(reports[format_id])
                )
                appended += 1
            except KeyError as exc:
                logging.warning(
                    f"Reports: {exc.args[0]} is not a valid format. Ignoring"
                )
        if not appended:
            logging.warning("Creating a default report as no valid were found")
            self.automation_config["jobs"].append(
                self._construct_report_af(reports["json"])
            )

    def _save_automation_file(self):
        """Save the Automation dictionary as YAML in the container"""
        af_host_path = self.path_map.workdir.host_path + "/af.yaml"
        with open(af_host_path, "w", encoding="utf-8") as f:
            f.write(yaml.dump(self.automation_config))
        logging.info(f"Saved Automation Framework in {af_host_path}")

    # Building an authentication factory for ZAP
    # For every authentication methods:
    # - Will extract authentication parameters from config's `scanners.zap.authentication.parameters`
    # - May modify `af` (e.g.: adding jobs, users)
    # - May add environment vars
    # - MUST return True if it created a user, and False otherwise
    @generic_authentication_factory("zap")
    def authentication_factory(self):
        """This is the default function, attached to error reporting"""
        raise RuntimeError(
            f"No valid authenticator found for ZAP. ZAP current config is: {self.config}"
        )

    @authentication_factory.register(None)
    def authentication_set_anonymous(self):
        """No authentication: don't do anything"""
        logging.info("ZAP NOT configured with any authentication")
        return False

    @authentication_factory.register("cookie")
    def authentication_set_cookie(self):
        """Configure authentication via HTTP Basic Authentication.
        Adds a 'Cookie: <name>=<value>' Header to every query

        Do this using the ZAP_AUTH_HEADER* environment vars

        Returns False as it does not create a ZAP user
        """
        params_path = "scanners.zap.authentication.parameters"
        cookie_name = self.config.get(f"{params_path}.name", None)
        cookie_val = self.config.get(f"{params_path}.value", None)

        self._add_env("ZAP_AUTH_HEADER", "Cookie")
        self._add_env("ZAP_AUTH_HEADER_VALUE", f"{cookie_name}={cookie_val}")

        logging.info("ZAP configured with Cookie authentication")
        return False

    @authentication_factory.register("http_header")
    def authentication_set_http_header_auth(self):
        """Configure authentication via a header name/value
        Adds a 'HeaderName: HeaderValue' to every query

        Do this using the ZAP_AUTH_HEADER* environment vars

        Returns False as it does not create a ZAP user
        """
        params_path = "scanners.zap.authentication.parameters"
        header_name = self.config.get(f"{params_path}.name", default="Authorization")
        header_val = self.config.get(f"{params_path}.value", default="")

        self._add_env("ZAP_AUTH_HEADER", header_name)
        self._add_env("ZAP_AUTH_HEADER_VALUE", header_val)

        logging.info("ZAP configured with Authentication using HTTP Header")
        return False

    @authentication_factory.register("http_basic")
    def authentication_set_http_basic_auth(self):
        """Configure authentication via HTTP Basic Authentication.
        Adds a 'Authorization: Basic <urlb64("{user}:{password}">' to every query

        Do this using the ZAP_AUTH_HEADER* environment vars

        Returns False as it does not create a ZAP user
        """
        params_path = "scanners.zap.authentication.parameters"
        username = self.config.get(f"{params_path}.username", None)
        password = self.config.get(f"{params_path}.password", None)

        blob = urlsafe_b64encode(f"{username}:{password}".encode()).decode("utf-8")

        self._add_env("ZAP_AUTH_HEADER", "Authorization")
        self._add_env("ZAP_AUTH_HEADER_VALUE", f"Basic {blob}")

        logging.info("ZAP configured with HTTP Basic Authentication")
        return False

    @authentication_factory.register("oauth2_rtoken")
    def authentication_set_oauth2_rtoken(self):
        """Configure authentication via OAuth2 Refresh Tokens
        In order to achieve that:
        - Create a ZAP user with username and refresh token
        - Sets the "script" authentication method in the ZAP Context
          - The script will request a new token when needed
        - Sets a "script" (httpsender) job, which will inject the latest
          token retrieved

        Returns True as it creates a ZAP user
        """

        context_ = find_context(self.automation_config)
        params_path = "scanners.zap.authentication.parameters"
        client_id = self.config.get(f"{params_path}.client_id", "cloud-services")
        token_endpoint = self.config.get(f"{params_path}.token_endpoint", None)
        rtoken = self.config.get(f"{params_path}.rtoken", None)
        scripts_dir = self.path_map.scripts.container_path

        # 1- complete the context: script, verification and user
        context_["authentication"] = {
            "method": "script",
            "parameters": {
                "script": f"{scripts_dir}/offline-token.js",
                "scriptEngine": "ECMAScript : Oracle Nashorn",
                "client_id": client_id,
                "token_endpoint": token_endpoint,
            },
            "verification": {
                "method": "response",
                "loggedOutRegex": "\\Q401\\E",
                "pollFrequency": 60,
                "pollUnits": "requests",
                "pollUrl": "",
                "pollPostData": "",
            },
        }
        context_["users"] = [
            {
                "name": Zap.USER,
                "credentials": {"refresh_token": "${RTOKEN}"},
            }
        ]
        # 2- add the name of the variable containing the token
        # The value will be taken from the environment at the time of starting
        self._add_env("RTOKEN", rtoken)

        # 2- complete the HTTPSender script job
        script = {
            "name": "script",
            "type": "script",
            "parameters": {
                "action": "add",
                "type": "httpsender",
                "engine": "ECMAScript : Oracle Nashorn",
                "name": "add-bearer-token",
                "file": f"{scripts_dir}/add-bearer-token.js",
                "target": "",
            },
        }
        self.automation_config["jobs"].append(script)
        logging.info("ZAP configured with OAuth2 RTOKEN")

        # quickhack: the openapi job currently does not run with user authentication.
        # This is a problem when openapi requires an authenticated URL.
        # => manually download the OAS, and change it to apiFile
        # This can be deleted when https://github.com/zaproxy/zaproxy/issues/7739 is resolved
        # Note: to avoid a temporary file, we download the file directly in its final destination in work_dir
        #       This is not a problem: it will simply be ignored by _include_file()
        oas_url = self.config.get("scanners.zap.apiScan.apis.apiUrl", default=None)
        if oas_url and self.config.get(
            "scanners.zap.miscOptions.oauth2OpenapiManualDownload", default=False
        ):
            logging.info("ZAP workaround: manually downloading the OpenAPI file")
            if authenticated_download_with_rtoken(
                url=oas_url,
                dest=f"{self._host_work_dir()}/openapi.json",
                auth={"rtoken": rtoken, "client_id": client_id, "url": token_endpoint},
                proxy=self.config.get("scanners.zap.proxy", default=None),
            ):
                logging.info(
                    "Successful manual download of the OAS: replacing apiUrl by apiFile"
                )
                self.config.set(
                    "scanners.zap.apiScan.apis.apiFile",
                    f"{self._host_work_dir()}/openapi.json",
                )
                self.config.delete("scanners.zap.apiScan.apis.apiUrl")
            else:
                logging.warning(
                    "Failed to manually download the OAS. delegating to ZAP"
                )

        return True

    ###############################################################
    # MAGIC METHODS                                               #
    # Special functions (other than __init__())                   #
    ###############################################################


# Given an Automation Framework configuration, return its sub-dictionary corresponding to the context we're going to use
def find_context(automation_config, context=Zap.DEFAULT_CONTEXT):
    # quick function that makes sure the context is sane
    def ensure_default(context2):
        # quick function that makes sure an entry is a list (override if necessary)
        def ensure_list(entry):
            if not context2.get(entry) or not isinstance(context2.get(entry), list):
                context2[entry] = []

        ensure_list("urls")
        ensure_list("includePaths")
        ensure_list("excludePaths")
        return context2

    try:
        for context3 in automation_config["env"]["contexts"]:
            if context3["name"] == context:
                return ensure_default(context3)
    except:
        pass
    logging.warning(
        f"No context matching {context} have ben found in the current Automation Framework configuration.",
        "It may be missing from default. An empty context is created",
    )
    # something failed: create an empty one and return it
    if not automation_config["env"]:
        automation_config["env"] = {}
    if not automation_config["env"].get("contexts"):
        automation_config["env"]["contexts"] = []
    automation_config["env"]["contexts"].append({"name": context})
    return ensure_default(automation_config["env"]["contexts"][-1])
