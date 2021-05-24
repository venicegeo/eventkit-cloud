# -*- coding: utf-8 -*-
from django.urls import re_path
from django.contrib.auth.decorators import login_required
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from eventkit_cloud.ui.views import (
    logout,
    auth,
    geocode,
    get_config,
    convert_to_geojson,
    user_active,
    reverse_geocode,
    convert,
    search,
)

urlpatterns = [
    re_path(
        r"^login",
        never_cache(ensure_csrf_cookie(TemplateView.as_view(template_name="ui/index.html"))),
        name="login",
    ),
    re_path(
        r"^$",
        never_cache(login_required(TemplateView.as_view(template_name="ui/index.html"))),
        name="home",
    ),
    re_path(
        r"^dashboard",
        never_cache(login_required(TemplateView.as_view(template_name="ui/index.html"))),
        name="dashboard",
    ),
    re_path(
        r"^exports/?$",
        never_cache(login_required(TemplateView.as_view(template_name="ui/index.html"))),
        name="exports",
    ),
    re_path(
        r"^status",
        never_cache(login_required(TemplateView.as_view(template_name="ui/index.html"))),
        name="status",
    ),
    re_path(
        r"^create/?$",
        never_cache(login_required(TemplateView.as_view(template_name="ui/index.html"))),
        name="create",
    ),
    re_path(
        r"^account",
        never_cache(login_required(TemplateView.as_view(template_name="ui/index.html"))),
        name="account",
    ),
    re_path(
        r"^about",
        never_cache(login_required(TemplateView.as_view(template_name="ui/index.html"))),
        name="about",
    ),
    re_path(
        r"^groups",
        never_cache(login_required(TemplateView.as_view(template_name="ui/index.html"))),
        name="groups",
    ),
    re_path(r"^logout", never_cache(login_required(logout)), name="logout"),
    re_path(r"^search/?$", never_cache(login_required(search))),
    re_path(r"^geocode/?$", never_cache(login_required(geocode))),
    re_path(r"^convert/?$", login_required(convert)),
    re_path(r"^reverse_geocode/?$", login_required(reverse_geocode)),
    re_path(r"^configuration/?$", never_cache(get_config)),
    re_path(r"^file_upload/?$", never_cache(login_required(convert_to_geojson))),
    re_path(r"^user_active$", never_cache(login_required(user_active)), name="user_active"),
]

urlpatterns += [
    re_path(r"^auth$", never_cache(ensure_csrf_cookie(auth)), name="auth"),
]
