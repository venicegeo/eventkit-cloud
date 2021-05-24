# -*- coding: utf-8 -*-


import json
import logging
import re
import xml.etree.ElementTree as ET
from io import StringIO
from typing import Type

import requests
from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry, Polygon, GeometryCollection
from django.utils.translation import ugettext as _
from enum import Enum
import yaml

from eventkit_cloud.utils import auth_requests
from eventkit_cloud.jobs.models import DataProvider
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


class CheckResults(Enum):
    """
    Enum describing possible results of the provider check. Returns are in JSON format, with a status field
    containing an error code, and a message field containing more detailed information. Status may be one of:
        TIMEOUT - The connection timed out (requests.get raised ConnectionTimeout)
        CONNECTION - Could not connect to endpoint (requests.get raised a different ConnectionError)
        NOT_FOUND - Server returned status 404; service is unlikely to be available
        SSL_EXCEPTION - Secure connection failed, probably due to a missing or misconfigured client cert/key
        UNAUTHORIZED - Not authorized to connect (response status 401 or 403)
        UNAVAILABLE - Server returned a status other than 200; service may not be available
        UNKNOWN_FORMAT - GetCapabilities returned blank, or unrecognized metadata format
        LAYER_NOT_AVAILABLE - The requested layer wasn't found among those listed by GetCapabilities reply
        NO_INTERSECT - The given AOI doesn't intersect the response's bounding box for the given layer
        NO_URL - No service url was given in the data provider config, so availability couldn't be checked
        UNKNOWN_ERROR - An exception was thrown that wasn't handled by any of the other Exception handlers.
        SUCCESS - No problems: export should proceed without issues
        (NB: for OWS sources in some cases, GetCapabilities may return 200 while GetMap/Coverage/Feature returns 403.
        In these cases, a success case will be falsely reported instead of ERR_UNAUTHORIZED.)
    """

    TIMEOUT = (
        {
            "status": "ERR",
            "type": "TIMEOUT",
            "message": _("Your connection has timed out; the provider may be offline. Refresh to try again."),
        },
    )

    CONNECTION = (
        {
            "status": "ERR",
            "type": "CONNECTION",
            "message": _("A connection to this data provider could not be established."),
        },
    )

    UNAUTHORIZED = (
        {
            "status": "ERR",
            "type": "UNAUTHORIZED",
            "message": _("Authorization is required to connect to this data provider."),
        },
    )

    NOT_FOUND = (
        {
            "status": "ERR",
            "type": "NOT_FOUND",
            "message": _("The data provider was not found on the server (status 404)."),
        },
    )

    SSL_EXCEPTION = (
        {
            "status": "WARN",
            "type": "SSL_EXCEPTION",
            "message": _("Could not connect securely to provider; possibly missing client certificate"),
        },
    )

    UNAVAILABLE = (
        {
            "status": "WARN",
            "type": "UNAVAILABLE",
            "message": _("This data provider may be unavailable (status %(status)s)."),
        },
    )

    UNKNOWN_FORMAT = (
        {
            "status": "WARN",
            "type": "UNKNOWN_FORMAT",
            "message": _(
                "This data provider returned metadata in an unexpected format; "
                "errors may occur when creating the DataPack."
            ),
        },
    )

    LAYER_NOT_AVAILABLE = (
        {
            "status": "WARN",
            "type": "LAYER_NOT_AVAILABLE",
            "message": _("This data provider does not offer the requested layer."),
        },
    )

    NO_INTERSECT = (
        {
            "status": "WARN",
            "type": "NO_INTERSECT",
            "message": _("The selected AOI does not intersect the data provider's layer."),
        },
    )

    NO_URL = (
        {
            "status": "WARN",
            "type": "NO_URL",
            "message": _("No Service URL was found in the data provider config; " "availability cannot be checked"),
        },
    )

    TOO_LARGE = (
        {
            "status": "WARN",
            "type": "SELECTION_TOO_LARGE",
            "message": _("The selected AOI is larger than the maximum allowed size for this data provider."),
        },
    )

    UNKNOWN_ERROR = (
        {"status": "ERR", "type": "ERROR", "message": _("An error has occurred, please contact an administrator.")},
    )

    SUCCESS = ({"status": "SUCCESS", "type": "SUCCESS", "message": _("Export should proceed without issues.")},)


class ProviderCheck(object):
    """
    During the second stage of creating a datapack, each available data provider is pinged to apprise the user
    of its availability. This class and its subclasses contain methods to ping OWS and Overpass servers, and determine:
        * Whether they are online
        * Whether authorization is required
        * Whether they are able to serve the requested layers; and
        * Whether the requested layer intersects the AOI of the DataPack to be created.
    Once returned, the information is displayed via an icon and tooltip in the EventKit UI.
    """

    def __init__(self, service_url, layer, aoi_geojson=None, slug=None, max_area=0, config: dict = None):
        """
        Initialize this ProviderCheck object with a service URL and layer.
        :param service_url: URL of provider, if applicable. Query string parameters are ignored.
        :param layer: Layer or coverage to check for
        :param aoi_geojson: (Optional) AOI to check for layer intersection
        :param slug: (Optional) A provider slug to use for getting credentials.
        :param max_area: The upper limit for this datasource.
        """

        self.service_url = service_url
        self.query = None
        self.layer = layer
        self.slug = slug
        self.max_area = max_area
        self.result = CheckResults.SUCCESS
        self.timeout = 10
        self.verify = getattr(settings, "SSL_VERIFICATION", True)
        self.config = config or dict()

        if aoi_geojson is not None and aoi_geojson != "":
            if isinstance(aoi_geojson, str):
                aoi_geojson = json.loads(aoi_geojson)

            geoms = tuple(
                [
                    GEOSGeometry(json.dumps(feature.get("geometry")), srid=4326)
                    for feature in aoi_geojson.get("features")
                ]
            )

            geom_collection = GeometryCollection(geoms, srid=4326)

            logger.debug("AOI: {}".format(json.dumps(aoi_geojson)))

            self.aoi = geom_collection
        else:
            self.aoi = None
            logger.debug("AOI was not given")

        self.token_dict = {}  # Parameters to include in message field of response

    def check_area(self):
        """
        Return True if the AOI selection's area is lower than the maximum for this provider, otherwise False.
        :return: True if AOI is lower than area limit
        """

        if self.aoi is None or int(self.max_area) <= 0:
            return True

        geom = self.aoi.transform(3857, clone=True)
        area = geom.area

        area_sq_km = area / 1000000

        return area_sq_km < self.max_area

    def get_check_response(self):
        """
        Sends a GET request to provider URL and returns its response if status code is ok
        """

        try:
            if not self.service_url:
                self.result = CheckResults.NO_URL
                return None

            cert_info = self.config.get("cert_info", None)
            response = auth_requests.get(
                self.service_url, cert_info=cert_info, params=self.query, timeout=self.timeout, verify=self.verify
            )

            self.token_dict["status"] = response.status_code

            if response.status_code in [401, 403]:
                self.result = CheckResults.UNAUTHORIZED
                return None

            if response.status_code == 404:
                self.result = CheckResults.NOT_FOUND
                return None

            if not response.ok:
                self.result = CheckResults.UNAVAILABLE
                return None

        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as ex:
            logger.error("Provider check timed out for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.TIMEOUT
            return None

        except requests.exceptions.SSLError as ex:
            logger.error("SSL connection failed for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.SSL_EXCEPTION
            return None

        except requests.exceptions.ConnectionError as ex:
            logger.error("Provider check failed for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.CONNECTION
            return None

        except Exception as ex:
            logger.error("An unknown error has occurred for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.UNKNOWN_ERROR
            return None

        return response

    def validate_response(self, response):
        """
        Given a 200 response, check it for validity (intersection, layer contents, etc).
        Base implementation always returns True if a response was given at all; subclasses may override
        :param response: requests.Response object
        :return: True if response is not None
        """
        return response is not None

    def check(self):
        """
        Main call to check the status of a provider. Returns JSON with a status string and more detailed message.
        """
        self.result = CheckResults.SUCCESS

        if self.check_area():
            response = self.get_check_response()
            if response is not None:
                self.validate_response(response)
        else:
            self.result = CheckResults.TOO_LARGE

        # TODO: This should not be dumped to a string here
        # The front end should NOT receive a string, it should get a valid JSON response.
        result_json = json.dumps(self.result.value[0]) % self.token_dict
        logger.debug("Provider check returning result: %s", result_json)
        return result_json


class OverpassProviderCheck(ProviderCheck):
    """
    Implementation of ProviderCheck for Overpass providers.
    """

    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)

    def get_check_response(self):
        """
        Sends a POST request for metadata to Overpass URL and returns its response if status code is ok
        """
        try:
            if not self.service_url:
                self.result = CheckResults.NO_URL
                return

            cert_info = self.config.get("cert_info")

            response = auth_requests.post(
                url=self.service_url, cert_info=cert_info, data="out meta;", timeout=self.timeout, verify=self.verify
            )

            self.token_dict["status"] = response.status_code

            if response.status_code in [401, 403]:
                self.result = CheckResults.UNAUTHORIZED
                return

            if response.status_code == 404:
                self.result = CheckResults.NOT_FOUND
                return

            if not response.ok:
                self.result = CheckResults.UNAVAILABLE
                return

        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as ex:
            logger.error("Provider check timed out for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.TIMEOUT
            return

        except requests.exceptions.SSLError as ex:
            logger.error("Provider check failed for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.SSL_EXCEPTION
            return

        except (requests.exceptions.ConnectionError, requests.exceptions.MissingSchema) as ex:
            logger.error("Provider check failed for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.CONNECTION
            return

        except Exception as ex:
            logger.error("An unknown error has occurred for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.UNKNOWN_ERROR
            return None


class OWSProviderCheck(ProviderCheck):
    """
    Implementation of ProviderCheck for OWS (WMS, WMTS, WFS, WCS) providers.
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize this OWSProviderCheck object with a service URL and layer.
        :param service_url: URL of provider, if applicable. Query string parameters are ignored.
        :param layer: Layer or coverage to check for
        :param aoi_geojson: (Optional) AOI to check for layer intersection
        """
        super(OWSProviderCheck, self).__init__(*args, **kwargs)

        self.query = {"VERSION": "1.0.0", "REQUEST": "GetCapabilities"}
        # Amended with "SERVICE" parameter by subclasses

        # If service or version parameters are left in query string, it can lead to a protocol error and false negative
        self.service_url = re.sub(r"(?i)(version|service|request)=.*?(&|$)", "", self.service_url)

        self.layer = self.layer.lower()

    def find_layer(self, root):
        raise NotImplementedError("Method is specific to provider type")

    def get_bbox(self, element):
        raise NotImplementedError("Method is specific to provider type")

    def get_layer_name(self):
        raise NotImplementedError("Method is specific to provider type")

    def check_intersection(self, bbox):
        """
        Given a bounding box, set result to NO_INTERSECT if it doesn't intersect the DataPack's AOI.
        :param bbox: Bounding box array: [minx, miny, maxx, maxy] in EPSG:4326
        """
        logger.debug("Data provider bbox: [minx, miny, maxx, maxy] = {}".format(str(bbox)))
        minx, miny, maxx, maxy = bbox
        bbox = Polygon.from_bbox((minx, miny, maxx, maxy))

        if self.aoi is not None and not self.aoi.intersects(bbox):
            self.result = CheckResults.NO_INTERSECT

    def validate_response(self, response):

        try:
            xml = response.content.decode()
            xmll = xml.lower()

            doctype = re.search(r"<!DOCTYPE[^>[]*(\[[^]]*\])?>", xml)
            if doctype is not None:
                doctype_pos = doctype.end()
                xmll = xml[:doctype_pos] + xml[doctype_pos + 1 :].lower()

            xmll = xmll.replace("![cdata[", "![CDATA[")

            # Strip namespaces from tags (from http://bugs.python.org/issue18304)
            iterator = ET.iterparse(StringIO(xmll))
            for event, element in iterator:
                if "}" in element.tag:
                    element.tag = element.tag.split("}", 1)[1]
            root = iterator.root
            layer_element = self.find_layer(root)

            if layer_element is None:
                return

            bbox = self.get_bbox(layer_element)
            if bbox is not None:
                self.check_intersection(bbox)

        except ET.ParseError as ex:
            logger.error("Provider check failed to parse GetCapabilities XML: {}".format(str(ex)))
            self.result = CheckResults.UNKNOWN_FORMAT
            return


class WCSProviderCheck(OWSProviderCheck):
    """
    Implementation of OWSProviderCheck for WCS providers
    """

    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)
        self.query["SERVICE"] = "WCS"

    def find_layer(self, root):
        """
        :param root: Name of layer to find
        :return: XML 'Layer' Element, or None if not found
        """

        content_meta = root.find(".//contentmetadata")
        if content_meta is None:
            self.result = CheckResults.UNKNOWN_FORMAT
            return None

        # Get names of available coverages
        coverage_offers = content_meta.findall("coverageofferingbrief")

        cover_names = [(c, c.find("name")) for c in coverage_offers]
        if not cover_names:  # No coverages are offered
            self.result = CheckResults.LAYER_NOT_AVAILABLE
            return None

        try:
            coverages = self.config.get("service", dict()).get("coverages")
            coverages = coverages.split(",") if coverages else None
            coverages = list(map(str.lower, coverages)) if coverages else None
        except AttributeError:
            logger.error("Unable to get coverages from WCS provider configuration.")

        if coverages:
            covers = [c for c, n in cover_names if n is not None and n.text in coverages]
        else:
            covers = [c for c, n in cover_names if n is not None and self.layer == n.text]

        if not covers:  # Requested coverage is not offered
            self.result = CheckResults.LAYER_NOT_AVAILABLE
            return None

        return covers

    def get_bbox(self, elements):
        bboxes = []
        for element in elements:
            envelope = element.find("lonlatenvelope")
            if envelope is None:
                continue

            pos = list(envelope)
            # Make sure there aren't any surprises
            coord_pattern = re.compile(r"^-?\d+(\.\d+)? -?\d+(\.\d+)?$")
            if not pos or not all("pos" in p.tag and re.match(coord_pattern, p.text) for p in pos):
                continue

            x1, y1 = list(map(float, pos[0].text.split(" ")))
            x2, y2 = list(map(float, pos[1].text.split(" ")))

            minx, maxx = sorted([x1, x2])
            miny, maxy = sorted([y1, y2])

            bboxes.append([minx, miny, maxx, maxy])
        return bboxes if bboxes else None

    def check_intersection(self, bboxes):
        """
        Given a list bounding boxes, set result to NO_INTERSECT if it doesn't intersect the DataPack's AOI.
        :param bboxes: list of bounding box arrays: [[minx, miny, maxx, maxy],..] in EPSG:4326
        """
        for box in bboxes:
            logger.debug("Data provider bbox: [minx, miny, maxx, maxy] = {}".format(str(box)))
            minx, miny, maxx, maxy = box
            bbox = Polygon.from_bbox((minx, miny, maxx, maxy))

            if not (self.aoi is not None and not self.aoi.intersects(bbox)):
                return

        self.result = CheckResults.NO_INTERSECT


class WFSProviderCheck(OWSProviderCheck):
    """
    Implementation of OWSProviderCheck for WFS providers
    """

    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)
        self.query["SERVICE"] = "WFS"

    def find_layer(self, root):
        """
        :param root: Name of layer to find
        :return: XML 'Layer' Element, or None if not found
        """
        feature_type_list = root.find(".//featuretypelist")
        if feature_type_list is None:
            self.result = CheckResults.UNKNOWN_FORMAT
            return None

        feature_types = feature_type_list.findall("featuretype")

        # Get layer names
        feature_names = [(ft, ft.find("name")) for ft in feature_types]
        logger.debug("WFS layers offered: {}".format([name.text for feature, name in feature_names if name]))
        features = [feature for feature, name in feature_names if name is not None and self.layer == name.text]

        if not features:
            self.result = CheckResults.LAYER_NOT_AVAILABLE
            return None

        feature = features[0]
        return feature

    def get_bbox(self, element):

        bbox_element = element.find("latlongboundingbox")

        if bbox_element is None:
            return None

        bbox = [float(bbox_element.attrib[point]) for point in ["minx", "miny", "maxx", "maxy"]]
        return bbox


class WMSProviderCheck(OWSProviderCheck):
    """
    Implementation of OWSProviderCheck for WMS providers
    """

    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)
        self.query["SERVICE"] = "WMS"

        # 1.3.0 will work as well, if that's returned. 1.0.0 isn't widely supported.
        self.query["VERSION"] = "1.1.1"

    def find_layer(self, root):
        """
        :param root: Name of layer to find
        :return: XML 'Layer' Element, or None if not found
        """
        capability = root.find(".//capability")
        if capability is None:
            self.result = CheckResults.UNKNOWN_FORMAT
            return None

        # Flatten nested layers to single list
        layers = capability.findall("layer")
        sublayers = layers
        while len(sublayers) > 0:
            sublayers = [layer for layer in sublayers for layer in layer.findall("layer")]
            layers.extend(sublayers)

        # Get layer names
        layer_names = [(layer, layer.find("name")) for layer in layers]
        logger.debug("WMS layers offered: {}".format([name.text for layer, name in layer_names if name]))

        requested_layer = self.get_layer_name()
        layer = [layer for layer, name in layer_names if name is not None and requested_layer == name.text]
        if not layer:
            self.result = CheckResults.LAYER_NOT_AVAILABLE
            return None

        layer = layer[0]
        return layer

    def get_bbox(self, element):

        bbox_element = element.find("latlonboundingbox")
        if bbox_element is not None:
            bbox = [float(bbox_element.attrib[point]) for point in ["minx", "miny", "maxx", "maxy"]]
            return bbox

        bbox_element = element.find("ex_geographicboundingbox")
        if bbox_element is not None:
            points = ["westboundlongitude", "southboundlatitude", "eastboundlongitude", "northboundlatitude"]
            bbox = [float(bbox_element.findtext(point)) for point in points]
            return bbox

        return None

    def get_layer_name(self):

        try:
            layer_name = (
                self.config.get("sources", {})
                .get("default", {})
                .get("req", {})
                .get("layers")  # TODO: Can there be more than one layer name in the WMS/WMTS config?
            )
        except AttributeError:
            logger.error("Unable to get layer name from provider configuration.")
            self.result = CheckResults.UNKNOWN_ERROR
            return None

        if layer_name is None:
            self.result = CheckResults.LAYER_NOT_AVAILABLE
            return None

        layer_name = str(layer_name).lower()
        return layer_name


class WMTSProviderCheck(OWSProviderCheck):
    """
    Implementation of OWSProviderCheck for WMS providers
    """

    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)
        self.query["SERVICE"] = "WMTS"

    def find_layer(self, root):
        """
        :param root: Name of layer to find
        :return: XML 'Layer' Element, or None if not found
        """
        contents = root.find(".//contents")
        if contents is None:
            self.result = CheckResults.UNKNOWN_FORMAT
            return None

        # Flatten nested layers to single list
        layers = contents.findall("layer")
        sublayers = layers
        while sublayers:
            sublayers = [layer for layer in sublayers for layer in layer.findall("layer")]
            layers.extend(sublayers)

        # Get layer names
        layer_names = [(layer, layer.find("identifier")) for layer in layers]
        logger.debug("WMTS layers offered: {}".format([name.text for layer, name in layer_names if name is not None]))
        requested_layer = self.get_layer_name()
        layer = [layer for layer, name in layer_names if name is not None and requested_layer == name.text]
        if not layer:
            self.result = CheckResults.LAYER_NOT_AVAILABLE
            return None

        layer = layer[0]
        return layer

    def get_bbox(self, element):

        bbox_element = element.find("wgs84boundingbox")

        if bbox_element is None:
            return None

        southwest = bbox_element.find("lowercorner").text.split()[::-1]
        northeast = bbox_element.find("uppercorner").text.split()[::-1]

        bbox = list(map(float, southwest + northeast))
        return bbox

    def get_layer_name(self) -> str:

        try:
            layer_name = (
                self.config.get("sources", {})
                .get("default", {})
                .get("req", {})
                .get("layers")  # TODO: Can there be more than one layer name in the WMS/WMTS config?
            )
        except AttributeError:
            logger.error("Unable to get layer name from provider configuration.")
            self.result = CheckResults.UNKNOWN_ERROR
            return None

        if layer_name is None:
            self.result = CheckResults.LAYER_NOT_AVAILABLE
            return None

        layer_name = layer_name.lower()
        return layer_name


class TMSProviderCheck(ProviderCheck):
    def __init__(self, service_url, *args, **kwargs):
        super(TMSProviderCheck, self).__init__(service_url, *args, **kwargs)
        self.service_url = self.service_url.format(z="0", y="0", x="0")


class FileProviderCheck(ProviderCheck):
    """
    Implementation of ProviderCheck for geospatial file providers.
    """

    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)

    def get_check_response(self):
        """
        Sends a HEAD request to the provided service URL returns its response if the status code is OK
        """
        try:
            if not self.service_url:
                self.result = CheckResults.NO_URL
                return

            cert_info = self.config.get("cert_info")

            response = auth_requests.head(
                url=self.service_url,
                cert_info=cert_info,
                timeout=self.timeout,
                verify=getattr(settings, "SSL_VERIFICATION", True),
            )

            self.token_dict["status"] = response.status_code

            if response.status_code in [401, 403]:
                self.result = CheckResults.UNAUTHORIZED
                return

            if response.status_code == 404:
                self.result = CheckResults.NOT_FOUND
                return

            if not response.ok:
                self.result = CheckResults.UNAVAILABLE
                return

        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as ex:
            logger.error("Provider check timed out for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.TIMEOUT
            return

        except requests.exceptions.SSLError as ex:
            logger.error("Provider check failed for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.SSL_EXCEPTION
            return

        except (requests.exceptions.ConnectionError, requests.exceptions.MissingSchema) as ex:
            logger.error("Provider check failed for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.CONNECTION
            return

        except Exception as ex:
            logger.error("An unknown error has occurred for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.UNKNOWN_ERROR
            return None


class OGCProviderCheck(ProviderCheck):
    """
    Implementation of ProviderCheck for geospatial file providers.
    """

    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)

    def get_check_response(self):
        """
        Sends a HEAD request to the provided service URL returns its response if the status code is OK
        """
        try:
            if not self.service_url:
                self.result = CheckResults.NO_URL
                return

            cert_info = self.config.get("cert_info")
            service_url = self.service_url.rstrip("/\\")
            processes_endpoint = urljoin(service_url, "processes/")

            response = auth_requests.get(
                url=processes_endpoint,
                cert_info=cert_info,
                timeout=self.timeout,
                verify=getattr(settings, "SSL_VERIFICATION", True),
            )

            self.token_dict["status"] = response.status_code

            if response.status_code in [401, 403]:
                self.result = CheckResults.UNAUTHORIZED
                return

            if response.status_code == 404:
                self.result = CheckResults.NOT_FOUND
                return

            if not response.ok:
                self.result = CheckResults.UNAVAILABLE
                return

        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as ex:
            logger.error("Provider check timed out for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.TIMEOUT
            return

        except requests.exceptions.SSLError as ex:
            logger.error("Provider check failed for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.SSL_EXCEPTION
            return

        except (requests.exceptions.ConnectionError, requests.exceptions.MissingSchema) as ex:
            logger.error("Provider check failed for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.CONNECTION
            return

        except Exception as ex:
            logger.error("An unknown error has occurred for URL {}: {}".format(self.service_url, str(ex)))
            self.result = CheckResults.UNKNOWN_ERROR
            return None


PROVIDER_CHECK_MAP = {
    "wfs": WFSProviderCheck,
    "wcs": WCSProviderCheck,
    "wms": WMSProviderCheck,
    "osm": OverpassProviderCheck,
    "osm-generic": OverpassProviderCheck,
    "wmts": WMTSProviderCheck,
    "arcgis-raster": ProviderCheck,
    "arcgis-feature": ProviderCheck,
    "tms": TMSProviderCheck,
    "vector-file": FileProviderCheck,
    "raster-file": FileProviderCheck,
    "ogcapi-process": OGCProviderCheck,
}


def get_provider_checker(type_slug) -> Type[ProviderCheck]:
    """
    Given a string describing a provider type, return a reference to the class appropriate for checking the status
    of a provider of that type.
    :param type_slug: String describing provider type
    :return: Checker for given provider type
    """
    try:
        return PROVIDER_CHECK_MAP[type_slug]
    except KeyError:
        return ProviderCheck


def perform_provider_check(provider: DataProvider, geojson) -> str:
    try:
        provider_type = str(provider.export_provider_type)

        url = str(provider.url)
        if url == "" and "osm" in provider_type:
            url = settings.OVERPASS_API_URL

        provider_checker = get_provider_checker(provider_type)
        conf = yaml.safe_load(provider.config) or dict()
        checker = provider_checker(
            service_url=url,
            layer=provider.layer,
            aoi_geojson=geojson,
            slug=provider.slug,
            max_area=provider.max_selection,
            config=conf,
        )
        response = checker.check()

    except Exception as e:
        response = json.dumps(CheckResults.UNKNOWN_ERROR.value[0])
        logger.info(f"An exception occurred while checking the {provider.name} provider: {e}")

    logger.info(f"Status of provider '{provider.name}': {response}")

    return response
