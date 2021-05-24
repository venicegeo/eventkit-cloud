# -*- coding: utf-8 -*-
import json
import logging

import requests
from django.conf import settings
from django.test import TransactionTestCase
from unittest.mock import patch, MagicMock

from eventkit_cloud.utils.provider_check import (
    WCSProviderCheck,
    WFSProviderCheck,
    WMSProviderCheck,
    WMTSProviderCheck,
    CheckResults,
)

logger = logging.getLogger(__name__)


def get_status(result):
    """
    Given a CheckResult, return its status ID.
    """
    return result.value[0]["status"]


class TestProviderCheck(TransactionTestCase):
    def setUp(
        self,
    ):
        self.path = settings.ABS_PATH()
        self.aoi_geojson = (
            '{"features": [{"geometry": {"type": "Polygon", "coordinates": '
            "[[ [0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0] ]]}}]}"
        )

    def check_ows(self, get, service_type, pc, invalid_content, empty_content, no_intersect_content, valid_content):
        """
        Checks the status of a WFS, WCS, WMS, or WMTS service.
        :param get: Patched requests.get
        :param pc: ProviderCheck instance
        :param invalid_content: XML representing an unrecognized response
        :param empty_content: XML representing response lacking requested layer
        :param no_intersect_content: XML representing response with requested layer that does not intersect AOI
        :param valid_content: XML representing response with requested layer that intersects AOI
        """
        # Test: cannot connect to server
        get.side_effect = requests.exceptions.ConnectionError()
        result_status = json.loads(pc.check())["status"]
        self.assertEqual(get_status(CheckResults.CONNECTION), result_status)

        # Test: server throws SSL exception
        get.side_effect = requests.exceptions.SSLError()
        result_status = json.loads(pc.check())["status"]
        self.assertEqual(get_status(CheckResults.SSL_EXCEPTION), result_status)

        # Test: server returns unauthorized response code
        get.side_effect = None
        response = MagicMock()
        response.content = ""
        response.status_code = 403
        response.ok = False
        get.return_value = response
        result_status = json.loads(pc.check())["status"]
        self.assertEqual(get_status(CheckResults.UNAUTHORIZED), result_status)

        # Test: server returns 404 response code
        response.status_code = 404
        get.return_value = response
        result_status = json.loads(pc.check())["status"]
        self.assertEqual(get_status(CheckResults.NOT_FOUND), result_status)

        # Test: server does not return recognizable xml
        response.content = invalid_content
        response.status_code = 200
        response.ok = True
        get.return_value = response
        result_status = json.loads(pc.check())["status"]
        self.assertEqual(get_status(CheckResults.UNKNOWN_FORMAT), result_status)

        if service_type not in ["wms", "wmts"]:  # TODO: fix layer checks for WMS/WMTS
            # Test: server does not offer the requested layer/coverage
            response.content = empty_content
            get.return_value = response
            result_status = json.loads(pc.check())["status"]
            self.assertEqual(get_status(CheckResults.LAYER_NOT_AVAILABLE), result_status)

        if service_type not in ["wms", "wmts"]:  # TODO: fix layer checks for WMS/WMTS
            # Test: requested layer/coverage does not intersect given AOI
            response.content = no_intersect_content
            get.return_value = response
            result_status = json.loads(pc.check())["status"]
            self.assertEqual(get_status(CheckResults.NO_INTERSECT), result_status)

        # Test: success
        response.content = valid_content
        get.return_value = response
        result_status = json.loads(pc.check())["status"]
        self.assertEqual(get_status(CheckResults.SUCCESS), result_status)

        # Test: no service_url was provided
        pc.service_url = ""
        result_status = json.loads(pc.check())["status"]
        self.assertEqual(get_status(CheckResults.NO_URL), result_status)

    @patch("eventkit_cloud.utils.provider_check.auth_requests.get")
    def test_check_wfs(self, get):
        url = "http://example.com/wfs?"
        layer = "exampleLayer"
        pc = WFSProviderCheck(url, layer, self.aoi_geojson)

        invalid_content = """<WFS_Capabilities xmlns="http://www.opengis.net/wfs">
                             </WFS_Capabilities>""".encode()
        empty_content = """<WFS_Capabilities xmlns="http://www.opengis.net/wfs">
                               <FeatureTypeList>
                               </FeatureTypeList>
                           </WFS_Capabilities>""".encode()
        no_intersect_content = """<WFS_Capabilities xmlns="http://www.opengis.net/wfs">
                                      <FeatureTypeList>
                                          <FeatureType>
                                              <Name>exampleLayer</Name>
                                              <LatLongBoundingBox maxx="11" maxy="11" minx="10" miny="10"/>
                                          </FeatureType>
                                      </FeatureTypeList>
                                  </WFS_Capabilities>""".encode()
        valid_content = """<WFS_Capabilities xmlns="http://www.opengis.net/wfs">
                               <FeatureTypeList>
                                   <FeatureType>
                                       <Name>exampleLayer</Name>
                                       <LatLongBoundingBox maxx="1" maxy="1" minx="-1" miny="-1"/>
                                   </FeatureType>
                               </FeatureTypeList>
                           </WFS_Capabilities>""".encode()

        self.check_ows(get, "wfs", pc, invalid_content, empty_content, no_intersect_content, valid_content)

    @patch("eventkit_cloud.utils.provider_check.auth_requests.get")
    def test_check_wcs(self, get):
        url = "http://example.com/wcs?"
        coverage = "exampleCoverage"
        pc = WCSProviderCheck(url, coverage, self.aoi_geojson)

        invalid_content = "".encode()
        empty_content = """<wcs:WCS_Capabilities xmlns:gml="http://www.opengis.net/gml" xmlns:wcs="http://www.opengis.net/wcs">
                               <wcs:ContentMetadata>
                               </wcs:ContentMetadata>
                           </wcs:WCS_Capabilities>""".encode()
        no_intersect_content = """<wcs:WCS_Capabilities xmlns:gml="http://www.opengis.net/gml" xmlns:wcs="http://www.opengis.net/wcs">
                                      <wcs:ContentMetadata>
                                          <wcs:CoverageOfferingBrief>
                                              <wcs:Name>exampleCoverage</wcs:Name>
                                              <wcs:lonLatEnvelope srsName="urn:ogc:def:crs:OGC:1.3:CRS84">
                                                   <gml:pos>10 10</gml:pos>
                                                   <gml:pos>11 11</gml:pos>
                                               </wcs:lonLatEnvelope>
                                          </wcs:CoverageOfferingBrief>
                                      </wcs:ContentMetadata>
                                  </wcs:WCS_Capabilities>""".encode()
        valid_content = """<wcs:WCS_Capabilities xmlns:gml="http://www.opengis.net/gml" xmlns:wcs="http://www.opengis.net/wcs">
                               <wcs:ContentMetadata>
                                   <wcs:CoverageOfferingBrief>
                                       <wcs:Name>exampleCoverage</wcs:Name>
                                       <wcs:lonLatEnvelope srsName="urn:ogc:def:crs:OGC:1.3:CRS84">
                                            <gml:pos>-1 -1</gml:pos>
                                            <gml:pos>1 1</gml:pos>
                                        </wcs:lonLatEnvelope>
                                   </wcs:CoverageOfferingBrief>
                               </wcs:ContentMetadata>
                           </wcs:WCS_Capabilities>""".encode()

        self.check_ows(get, "wcs", pc, invalid_content, empty_content, no_intersect_content, valid_content)

    @patch("eventkit_cloud.utils.provider_check.auth_requests.get")
    def test_check_wms(self, get):
        url = "http://example.com/wms?"
        layer = "exampleLayer"
        config = {"sources": {"default": {"req": {"layers": layer}}}}
        pc = WMSProviderCheck(url, layer, self.aoi_geojson, config=config)
        invalid_content = "".encode()
        empty_content = """<WMT_MS_Capabilities version="1.1.1">
                               <Capability>
                               </Capability>
                           </WMT_MS_Capabilities>""".encode()

        no_intersect_content = """<WMT_MS_Capabilities version="1.1.1">
                                      <Capability>
                                          <Layer>
                                              <Layer>
                                                  <Name>exampleLayer</Name>
                                                  <LatLonBoundingBox minx="10" miny="10" maxx="11" maxy="11"/>
                                              </Layer>
                                          </Layer>
                                      </Capability>
                                  </WMT_MS_Capabilities>""".encode()

        valid_content = """<WMT_MS_Capabilities version="1.1.1">
                               <Capability>
                                   <Layer>
                                       <Name>exampleLayer</Name>
                                       <LatLonBoundingBox minx="-1" miny="-1" maxx="1" maxy="1"/>
                                   </Layer>
                               </Capability>
                           </WMT_MS_Capabilities>""".encode()

        self.check_ows(get, "wms", pc, invalid_content, empty_content, no_intersect_content, valid_content)

        pc = WMSProviderCheck(url, layer, self.aoi_geojson, config=config)
        valid_content = """<WMT_MS_Capabilities version="1.3.0">
                        <Capability>
                            <Layer>
                                <Name>exampleLayer</Name>
                                <EX_GeographicBoundingBox>
                                    <westBoundLongitude>-1</westBoundLongitude>
                                    <eastBoundLongitude>1</eastBoundLongitude>
                                    <southBoundLatitude>-1</southBoundLatitude>
                                    <northBoundLatitude>1</northBoundLatitude>
                                </EX_GeographicBoundingBox>
                            </Layer>
                        </Capability>
                    </WMT_MS_Capabilities>""".encode()

        self.check_ows(get, "wms", pc, invalid_content, empty_content, no_intersect_content, valid_content)

    @patch("eventkit_cloud.utils.provider_check.auth_requests.get")
    def test_check_wmts(self, get):
        url = "http://example.com/wmts?"
        layer = "exampleLayer"
        config = {"sources": {"default": {"req": {"layers": layer}}}}
        pc = WMTSProviderCheck(url, layer, self.aoi_geojson, config=config)

        invalid_content = "".encode()
        empty_content = """<Capabilities version="1.0.0">
                               <Contents>
                               </Contents>
                           </Capabilities>""".encode()

        no_intersect_content = """<Capabilities version="1.0.0">
                                      <Contents>
                                          <Layer>
                                              <Layer>
                                                  <Title>exampleLayer</Title>
                                                  <WGS84BoundingBox>
                                                       <LowerCorner>10 10</LowerCorner>
                                                       <UpperCorner>11 11</UpperCorner>
                                                  </WGS84BoundingBox>
                                              </Layer>
                                          </Layer>
                                      </Contents>
                                  </Capabilities>""".encode()

        valid_content = """<Capabilities version="1.0.0">
                               <Contents>
                                   <Layer>
                                       <Identifier>exampleLayer</Identifier>
                                       <WGS84BoundingBox>
                                            <LowerCorner>-1 -1</LowerCorner>
                                            <UpperCorner>1 1</UpperCorner>
                                       </WGS84BoundingBox>
                                   </Layer>
                               </Contents>
                           </Capabilities>""".encode()

        self.check_ows(get, "wmts", pc, invalid_content, empty_content, no_intersect_content, valid_content)
