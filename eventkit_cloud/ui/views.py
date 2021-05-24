# -*- coding: utf-8 -*-
"""UI view definitions."""
import json
from datetime import timedelta
from logging import getLogger

from django.conf import settings
from django.contrib.auth import authenticate, login
from django.contrib.auth import logout as auth_logout
from django.urls import reverse
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.context_processors import csrf
from django.views.decorators.http import require_http_methods
from rest_framework.renderers import JSONRenderer

from eventkit_cloud.api.serializers import UserDataSerializer
from eventkit_cloud.ui.helpers import (
    file_to_geojson,
    set_session_user_last_active_at,
    is_mgrs,
    is_lat_lon,
)
from eventkit_cloud.utils.geocoding.coordinate_converter import CoordinateConverter
from eventkit_cloud.utils.geocoding.geocode import Geocode
from eventkit_cloud.utils.geocoding.reverse import ReverseGeocode

logger = getLogger(__file__)


@require_http_methods(["GET"])
def create_export(request):
    """
    Handles display of the create export page.
    """
    user = request.user
    max_extent = {"extent": settings.JOB_MAX_EXTENT}
    for group in user.groups.all():
        if hasattr(group, "export_profile"):
            max_extent["extent"] = group.export_profile.max_extent
    extent = max_extent.get("extent")
    context = {"user": user, "max_extent": extent}
    context.update(csrf(request))
    return render(request, "ui/create.html", context)


# @user_verification_required
@require_http_methods(["GET"])
def clone_export(request, uuid=None):
    """
    Handles display of the clone export page.
    """
    max_extent = {"extent": settings.JOB_MAX_EXTENT}  # default
    user = request.user
    for group in user.groups.all():
        if hasattr(group, "export_profile"):
            max_extent["extent"] = group.export_profile.max_extent
    extent = max_extent.get("extent")
    context = {"user": user, "max_extent": extent}
    context.update(csrf(request))
    return render(request, "ui/clone.html", context)


# @user_verification_required
@require_http_methods(["GET"])
def view_export(request, uuid=None):  # NOQA
    """
    Handles display of the clone export page.
    """
    user = request.user
    context = {"user": user}
    return render(request, "ui/detail.html", context)


def auth(request):
    if (request.method == "GET") and request.user.is_authenticated:
        # If the user is already authenticated we want to return the user data (required for oauth).
        return HttpResponse(
            JSONRenderer().render(UserDataSerializer(request.user, context={"request": request}).data),
            content_type="application/json",
            status=200,
        )
    elif getattr(settings, "LDAP_SERVER_URI", getattr(settings, "DJANGO_MODEL_LOGIN")):
        if request.method == "POST":
            """Logs out user"""
            auth_logout(request)
            username = request.POST.get("username")
            password = request.POST.get("password")
            user_data = authenticate(username=username, password=password)
            if user_data is None:
                return HttpResponse(status=401)
            else:
                login(request, user_data)
                set_session_user_last_active_at(request)
                return HttpResponse(
                    JSONRenderer().render(UserDataSerializer(user_data, context={"request": request}).data),
                    content_type="application/json",
                    status=200,
                )
        if request.method == "GET":
            # We want to return a 200 so that the frontend can decide if the auth endpoint is valid for displaying the
            # the login form.
            return HttpResponse(status=200)
    else:
        return HttpResponse(status=400)


def logout(request):
    """Logs out user"""
    auth_logout(request)
    response = redirect("login")
    if settings.SESSION_USER_LAST_ACTIVE_AT in request.session:
        del request.session[settings.SESSION_USER_LAST_ACTIVE_AT]
    response.delete_cookie(settings.AUTO_LOGOUT_COOKIE_NAME, domain=settings.SESSION_COOKIE_DOMAIN)
    return response


def require_email(request):
    """
    View to handle email collection for new user log in with OSM account.
    """
    backend = request.session["partial_pipeline"]["backend"]
    return render(request, "osm/email.html", {"backend": backend})


@require_http_methods(["GET"])
def search(request):
    """
    Detects the query type and calls the relevant geocoder to get results
    :param request: User request which should include a query parameter
    :return: A geojson with features matching the search query
    """
    q = request.GET.get("query", None)
    if not q:
        return HttpResponse(status=204, content_type="application/json")

    error_string = "An unknown error occurred while querying for results, please contact an administrator."
    degree_range = 0.05
    if is_mgrs(q):
        # check for necessary settings
        if getattr(settings, "CONVERT_API_URL") is None:
            return HttpResponse("No Convert API specified", status=501)

        if getattr(settings, "REVERSE_GEOCODING_API_URL") is None:
            return HttpResponse("No Reverse Geocode API specified", status=501)

        # make call to convert which should return a geojson feature of the MGRS location
        convert = CoordinateConverter()
        try:
            mgrs_data = convert.get(q)
        except Exception as e:
            logger.error(e)
            return HttpResponse(content=error_string, status=500)

        # if no feature geom return nothing
        if not mgrs_data or not mgrs_data.get("geometry"):
            return HttpResponse(status=204, content_type="application/json")

        features = []
        # save the mgrs feature to return later
        if not mgrs_data.get("properties"):
            mgrs_data["properties"] = {}
        mgrs_data["properties"]["bbox"] = [
            mgrs_data.get("geometry").get("coordinates")[0] - degree_range,
            mgrs_data.get("geometry").get("coordinates")[1] - degree_range,
            mgrs_data.get("geometry").get("coordinates")[0] + degree_range,
            mgrs_data.get("geometry").get("coordinates")[1] + degree_range,
        ]
        mgrs_data["source"] = "MGRS"
        features.append(mgrs_data)

        # call reverse to get a list of results near the mgrs feature
        reverse = ReverseGeocode()
        try:
            result = reverse.search(
                {
                    "lat": mgrs_data.get("geometry").get("coordinates")[1],
                    "lon": mgrs_data.get("geometry").get("coordinates")[0],
                }
            )
        except Exception as e:
            logger.error(e)
            return HttpResponse(content=error_string, status=500)

        if result.get("features"):
            # add the mgrs feature with the search results and return together
            result["features"] = features + result["features"]
            return HttpResponse(content=json.dumps(result), status=200, content_type="application/json")
        # if no results just return the MGRS feature in the response
        return HttpResponse(
            content=json.dumps({"features": features}),
            status=200,
            content_type="application/json",
        )

    elif is_lat_lon(q):
        coords = is_lat_lon(q)
        # if no reverse url return 501
        if getattr(settings, "REVERSE_GEOCODING_API_URL") is None:
            return HttpResponse("No Reverse Geocode API specified", status=501)

        # make call to reverse geocode
        reverse = ReverseGeocode()
        try:
            result = reverse.search({"lat": coords[0], "lon": coords[1]})
        except Exception:
            return HttpResponse(content=error_string, status=500)

        # create a feature representing the exact lat/lon being searched
        point_feature = {
            "geometry": {"type": "Point", "coordinates": [coords[1], coords[0]]},
            "source": "Coordinate",
            "type": "Feature",
            "properties": {
                "name": "{0} {1}, {2} {3}".format(
                    coords[0] if coords[0] >= 0 else coords[0] * -1,
                    "N" if coords[0] >= 0 else "S",
                    coords[1] if coords[1] >= 0 else coords[1] * -1,
                    "E" if coords[1] >= 0 else "W",
                ),
                "bbox": [
                    coords[1] - degree_range,
                    coords[0] - degree_range,
                    coords[1] + degree_range,
                    coords[0] + degree_range,
                ],
            },
        }
        # if there are results add the point feature and return them together
        if result.get("features"):
            result.get("features").insert(0, point_feature)
            return HttpResponse(content=json.dumps(result), status=200, content_type="application/json")
        # if there are no results return only the point feature
        features = {"features": [point_feature]}
        return HttpResponse(content=json.dumps(features), status=200, content_type="application/json")
    else:
        # make call to geocode with search
        geocode = Geocode()
        try:
            result = geocode.search(q)
        except Exception as e:
            logger.error(e)
            return HttpResponse(content=error_string, status=500)
        return HttpResponse(content=json.dumps(result), status=200, content_type="application/json")


@require_http_methods(["GET"])
def geocode(request):
    geocode = Geocode()
    if request.GET.get("search"):
        result = geocode.search(request.GET.get("search"))
        return HttpResponse(content=json.dumps(result), status=200, content_type="application/json")
    if request.GET.get("result"):
        result = geocode.add_bbox(json.loads(request.GET.get("result")))
        return HttpResponse(content=json.dumps(result), status=200, content_type="application/json")
    else:
        return HttpResponse(status=204, content_type="application/json")


@require_http_methods(["GET"])
def convert(request):
    convert = CoordinateConverter()
    if getattr(settings, "CONVERT_API_URL") is not None:
        if request.GET.get("convert"):
            result = convert.get(request.GET.get("convert"))
            return HttpResponse(content=json.dumps(result), status=200, content_type="application/json")
        else:
            return HttpResponse(status=204, content_type="application/json")
    else:
        return HttpResponse("No Convert API specified", status=501)


@require_http_methods(["GET"])
def reverse_geocode(request):
    reverseGeocode = ReverseGeocode()
    if getattr(settings, "REVERSE_GEOCODING_API_URL") is not None:
        if request.GET.get("lat") and request.GET.get("lon"):
            result = reverseGeocode.search({"lat": request.GET.get("lat"), "lon": request.GET.get("lon")})
            return HttpResponse(content=json.dumps(result), status=200, content_type="application/json")
        if request.GET.get("result"):
            result = reverseGeocode.add_bbox(json.loads(request.GET.get("result")))
            return HttpResponse(content=json.dumps(result), status=200, content_type="application/json")
        else:
            return HttpResponse(status=204, content_type="application/json")
    else:
        return HttpResponse("No Reverse Geocode API specified", status=501)


@require_http_methods(["GET"])
def about(request):
    exports_url = reverse("list")
    help_url = reverse("help")
    return render(request, "ui/about.html", {"exports_url": exports_url, "help_url": help_url})


@require_http_methods(["GET"])
def help_main(request):
    return render(request, "help/help.html", {})


@require_http_methods(["GET"])
def help_create(request):
    create_url = reverse("create")
    help_features_url = reverse("help_features")
    return render(request, "help/help_create.html", {"create_url": create_url, "help_features_url": help_features_url})


@require_http_methods(["GET"])
def help_features(request):
    return render(request, "help/help_features.html", {})


@require_http_methods(["GET"])
def help_exports(request):
    export_url = reverse("list")
    return render(request, "help/help_exports.html", {"export_url": export_url})


@require_http_methods(["GET"])
def help_formats(request):
    return render(request, "help/help_formats.html", {})


@require_http_methods(["GET"])
def help_presets(request):
    configurations_url = reverse("configurations")
    return render(
        request,
        "help/help_presets.html",
        {"configurations_url": configurations_url},
    )


@require_http_methods(["GET"])
def get_config(request):
    """
    :param request: a GET request
    :return: a dict of available configurations
    """
    config = getattr(settings, "UI_CONFIG", {})
    return HttpResponse(json.dumps(config), content_type="application/json", status=200)


@require_http_methods(["POST"])
def convert_to_geojson(request):
    file = request.FILES.get("file", None)
    if not file:
        return HttpResponse("No file supplied in the POST request", status=400)
    try:
        geojson = file_to_geojson(file)
        return HttpResponse(json.dumps(geojson), content_type="application/json", status=200)
    except Exception as e:
        logger.error(e)
        return HttpResponse(str(e), status=400)


def user_active(request):
    """Prevents auto logout by updating the session's last active time"""
    # If auto logout is disabled, just return an empty body.
    if not settings.AUTO_LOGOUT_SECONDS:
        return HttpResponse(json.dumps({}), content_type="application/json", status=200)

    last_active_at = set_session_user_last_active_at(request)
    auto_logout_at = last_active_at + timedelta(seconds=settings.AUTO_LOGOUT_SECONDS)
    auto_logout_warning_at = auto_logout_at - timedelta(seconds=settings.AUTO_LOGOUT_WARNING_AT_SECONDS_LEFT)
    return HttpResponse(
        json.dumps(
            {
                "auto_logout_at": auto_logout_at.isoformat(),
                "auto_logout_warning_at": auto_logout_warning_at.isoformat(),
            }
        ),
        content_type="application/json",
        status=200,
    )


# error views
@require_http_methods(["GET"])
def create_error_view(request):
    return render(request, "ui/error.html", {}, status=500)


def internal_error_view(request):
    return render(request, "ui/500.html", {}, status=500)


def not_found_error_view(request):
    return render(request, "ui/404.html", {}, status=404)


def not_allowed_error_view(request):
    return render(request, "ui/403.html", {}, status=403)
