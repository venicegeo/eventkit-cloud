import json
import logging
import os

from django import forms
from django.conf.urls import url
from django.contrib import admin
from django.contrib import messages
from django.contrib.gis.admin import OSMGeoAdmin
from django.shortcuts import render
from django.utils.html import format_html
from django_celery_beat.models import IntervalSchedule, CrontabSchedule

from eventkit_cloud.jobs.forms import RegionForm, RegionalPolicyForm
from eventkit_cloud.jobs.models import (
    ExportFormat,
    Projection,
    Job,
    Region,
    RegionalPolicy,
    RegionalJustification,
    DataProvider,
    DataProviderType,
    DatamodelPreset,
    License,
    DataProviderStatus,
    DataProviderTask,
    JobPermission,
)
from eventkit_cloud.jobs.helpers import clean_config

logger = logging.getLogger(__name__)

admin.site.register(ExportFormat)
admin.site.register(Projection)
admin.site.register(DataProviderType)
admin.site.register(DatamodelPreset)
admin.site.register(License)
admin.site.register(DataProviderTask)


class JobAdmin(OSMGeoAdmin):
    """
    Admin model for editing Jobs in the admin interface.
    """

    search_fields = ["uid", "name", "user__username", "region__name"]
    list_display = ["uid", "name", "user", "region"]
    readonly_fields = ["user", "name", "description", "event"]
    exclude = [
        "the_geom",
        "the_geom_webmercator",
        "original_selection",
        "the_geog",
        "data_provider_tasks",
        "json_tags",
        "preset",
    ]
    actions = ["select_exports"]

    update_template = "admin/update_regions.html"
    update_complete_template = "admin/update_complete.html"

    def select_exports(self, request):
        """
        Select exports to update.
        """
        selected = ",".join(request.POST.getlist(admin.ACTION_CHECKBOX_NAME))
        regions = Region.objects.all()

        # noinspection PyProtectedMember
        return render(
            request,
            self.update_template,
            {"regions": regions, "selected": selected, "opts": self.model._meta},
        )

    select_exports.short_description = "Assign a region to the selected exports"

    def update_exports(self, request):
        """
        Update selected exports.
        """
        selected = request.POST.get("selected", "")
        num_selected = len(selected.split(","))
        region_uid = request.POST.get("region", "")
        region = Region.objects.get(uid=region_uid)
        for selected_id in selected.split(","):
            export = Job.objects.get(id=selected_id)
            export.region = region
            export.save()

        messages.success(request, "{0} exports updated.".format(num_selected))
        # noinspection PyProtectedMember
        return render(
            request,
            self.update_complete_template,
            {"num_selected": len(selected.split(",")), "region": region.name, "opts": self.model._meta},
        )

    def get_urls(self):
        urls = super(JobAdmin, self).get_urls()
        update_urls = [
            url(r"^select/$", self.admin_site.admin_view(self.select_exports)),
            url(
                r"^update/$",
                self.admin_site.admin_view(self.update_exports),
                name="update_regions",
            ),
        ]
        return update_urls + urls


class ExportConfigAdmin(admin.ModelAdmin):
    """
    Admin model for editing export configurations in the admin interface.
    """

    search_fields = ["uid", "name", "user__username"]
    list_display = ["uid", "name", "user", "config_type", "published", "created_at"]


class DataProviderForm(forms.ModelForm):
    """
    Admin form for editing export providers in the admin interface.
    """

    class Meta:
        model = DataProvider
        fields = [
            "name",
            "slug",
            "label",
            "url",
            "preview_url",
            "service_copyright",
            "service_description",
            "data_type",
            "layer",
            "export_provider_type",
            "max_selection",
            "level_from",
            "level_to",
            "config",
            "user",
            "license",
            "zip",
            "display",
            "attribute_class",
        ]

    def clean_config(self):
        config = self.cleaned_data.get("config")

        service_type = self.cleaned_data.get("export_provider_type").type_name

        if service_type in ["wms", "wmts", "tms", "arcgis-raster"]:
            from eventkit_cloud.utils.mapproxy import (
                MapproxyGeopackage,
                ConfigurationError,
            )

            service = MapproxyGeopackage(
                layer=self.cleaned_data.get("layer"),
                service_type=self.cleaned_data.get("export_provider_type"),
                config=config,
            )
            try:
                service.get_check_config()
            except ConfigurationError as e:
                raise forms.ValidationError(str(e))

        elif service_type in ["osm", "osm-generic"]:
            if not config:
                raise forms.ValidationError("Configuration is required for OSM data providers")
            from eventkit_cloud.feature_selection.feature_selection import FeatureSelection

            cleaned_config = clean_config(config)
            feature_selection = FeatureSelection(cleaned_config)
            feature_selection.valid
            if feature_selection.errors:
                raise forms.ValidationError("Invalid configuration: {0}".format(feature_selection.errors))

        return config


class DataProviderAdmin(admin.ModelAdmin):
    """
    Admin model for editing export providers in the admin interface.
    """

    form = DataProviderForm
    list_display = [
        "name",
        "slug",
        "label",
        "export_provider_type",
        "attribute_class",
        "license",
        "display",
    ]


# The reason for these empty classes is to remove IntervalSchedule and CrontabSchedule from the admin page. The easiest
# way to do this is to unregister them using admin.site.unregister, but that also means that you can't use the plus
# button to add new ones on lists displayed on admin pages of other models (in this case, PeriodicTask). Having the
# model be registered but hidden prevents that option from being removed.
class IntervalScheduleAdmin(admin.ModelAdmin):
    def get_model_perms(self, request):
        return {}


class CrontabScheduleAdmin(admin.ModelAdmin):
    def get_model_perms(self, request):
        return {}


admin.site.unregister(IntervalSchedule)
admin.site.unregister(CrontabSchedule)


class DataProviderStatusAdmin(admin.ModelAdmin):
    """
    Status information for Data Providers
    """

    def color_status(self, obj):
        if obj.status == "SUCCESS":
            return format_html(
                '<div style="width:100%%; height:100%%; background-color:rgba(0, 255, 0, 0.3);">%s</div>' % obj.status
            )
        elif obj.status.startswith("WARN"):
            return format_html(
                '<div style="width:100%%; height:100%%; background-color:rgba(255, 255, 0, 0.3);">%s</div>' % obj.status
            )
        return format_html(
            '<div style="width:100%%; height:100%%; background-color:rgba(255, 0, 0, 0.3);">%s</div>' % obj.status
        )

    color_status.short_description = "status"

    model = DataProviderStatus
    readonly_fields = (
        "status",
        "status_type",
        "message",
        "last_check_time",
        "related_provider",
    )
    list_display = (
        "color_status",
        "status_type",
        "message",
        "last_check_time",
        "related_provider",
    )
    list_filter = ("related_provider", "status", "status_type", "last_check_time")

    def has_add_permission(self, request, obj=None):
        return False


def get_example_from_file(file_path: str):
    with open(os.path.join(os.path.dirname(__file__), file_path)) as json_file:
        return json.dumps(json.load(json_file))


class RegionAdmin(admin.ModelAdmin):
    model = Region
    list_display = ("uid", "name")
    form = RegionForm


class RegionalPolicyAdmin(admin.ModelAdmin):
    model = RegionalPolicy
    form = RegionalPolicyForm
    list_display = ("uid", "name", "region")

    fieldsets = (
        (None, {"fields": ["name", "region", "providers"]}),
        (
            None,
            {
                "fields": [
                    "policies",
                    "policy_title_text",
                    "policy_header_text",
                    "policy_footer_text",
                    "policy_cancel_text",
                    "policy_cancel_button_text",
                ],
                "description": "The policy field expects a JSON structure with a list of policy objects. "
                "Each policy object must contain a title and a description. See the example below."
                f"<br /> <br /> {get_example_from_file('examples/policies_example.json')}",
            },
        ),
        (
            None,
            {
                "fields": ["justification_options"],
                "description": "The justification options field expects a JSON structure with a "
                "list of option objects. Each option object must have an integer id, string name "
                "and boolean display. Options may also have suboptions. "
                "Suboptions can be of type text or dropdown.  See the example below."
                f"<br /> <br /> {get_example_from_file('examples/justification_options_example.json')}",
            },
        ),
    )


class RegionalJustificationAdmin(admin.ModelAdmin):
    model = RegionalJustification
    list_display = ("uid", "justification_id", "justification_name", "regional_policy", "user")

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return [field.name for field in obj._meta.get_fields()]
        return self.readonly_fields

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# register the new admin models
admin.site.register(IntervalSchedule, IntervalScheduleAdmin)
admin.site.register(CrontabSchedule, CrontabScheduleAdmin)
admin.site.register(Job, JobAdmin)
admin.site.register(DataProvider, DataProviderAdmin)
admin.site.register(DataProviderStatus, DataProviderStatusAdmin)
admin.site.register(Region, RegionAdmin)
admin.site.register(RegionalPolicy, RegionalPolicyAdmin)
admin.site.register(RegionalJustification, RegionalJustificationAdmin)
admin.site.register(JobPermission)
