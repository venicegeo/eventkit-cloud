
#Administration

The Eventkit admin console allows an administrator to create and modify providers, manage user accounts, monitor jobs and tasks, set authentication tokens, and manage licenses.

##Tokens

To add a token for a user, go to the **Auth Token > Tokens**. When a user name is given, an authentication token is created for that user. Tokens may also be deleted at any time by an administrator with the appropriate permissions.

##Users and Groups

Admins may create and modify user accounts, changing names and resetting passwords.

###Adding a User

To add a new user, click "ADD USER +" in the upper right of the **Authentication and Authorization > Users** - section, and enter a username and password. Usernames can be no longer than 150 characters, and are limited to letters, digits, and the characters `@.+-_`. There are no specific requirements for the password field.

Other user-related fields, such as email address and first/last names, may be set using the Change User form, detailed below.

###Modifying a User

In the **Authentication and Authorization > Users** - section, all users are listed, and a filter is available to display by staff status, superuser status, activity, and group membership. To change a user's information, select the user in the **Authentication and Authorization > Users** - section.

Options that are available to view or change include:

* **Username**
* **Password** - Since raw passwords are not stored, passwords cannot be viewed. Instead, a hash and salt are shown, along with information on the algorithm that was used. Passwords may be reset by following the link in the label underneath the password information.
* **First name** - Optional
* **Last name** - Optional
* **Email address** - Optional
* **Active status** - Uncheck this to prevent a user from logging in, preferred to deleting accounts.
* **Staff status** - Check this to allow a user to log into the admin site. This does not automatically include all admin permissions, but is necessary for any of them to be exercised.
* **Superuser status** - Check this to allow a user all available permissions without needing to explicitly assign them.
* **Groups** - Users may be assigned to one or more groups, which may confer administrative permissions.
* **User permissions** - Users may be assigned administrative permissions on an individual basis, or via groups (see below).
* **Last login** - The date and time of the user's last login to EventKit are shown here, in server time.
* **Date joined** - The date and time that the user was first created are shown here, in server time.

###Groups

Users' administrative permissions may be defined for individual accounts, or by groups. By default, EventKit users are assigned to the `DefaultExportExtentGroup`, which has no administrative privileges; other groups may be added with access to specific sections of the administrative console, such as the ability to add or change an export provider, or to delete jobs.

To add a permission to a group, select that group within the **Authentication and Authorization > Groups** section, locate and select the permission on the left side of the double list box, labeled "Available permissions", and click the right-facing arrow between the boxes. To remove a permission, select it in the "Chosen permissions" box and click the left-facing arrow.

##Celery

Through Celery Beat, jobs can be scheduled for periodic runs.

###Crontabs

The **Django Celery Beat > Crontabs** section lists all of the times at which activity is scheduled, in the standard [crontab format](http://crontab.org/). Crontabs that are defined here may be assigned to tasks.

###Intervals

As an alternative to crontabs, intervals may be specified to run a job regularly, with an interval 0 < N < 2147483648 and units of microseconds, seconds, minutes, hours, or days.

###Periodic tasks

Periodic tasks are listed under **Django Celery Beat > Periodic Tasks**. The parameters for each task include:

**Name** - Descriptive name for the task  
**Task (registered)** - A task selected from a list of all tasks registered in EventKit's Celery backend  
**Task (custom)** - If the task isn't registered, a name for the custom task  
**Enabled** - Uncheck to disable this task and prevent it from running  

Either an interval or a crontab must be defined for each task.

**Interval** - A predefined interval (above) at which to run this task  
**Crontab** - A predefined crontab (above) under which to run this task  

**Arguments** - Custom arguments to pass to this task, if applicable  
**Keyword arguments** - Custom keyword arguments to pass to this task, if applicable  

**Expires** - Date and time at which this periodic task should expire, if desired  
**Queue**  
**Exchange**  
**Routing key**  

##Jobs

###Datamodel Presets

These contain tag definitions for OSM feature providers, describing features by broad and specific categories. By default, OSM and HDM (Humanitarian Data Model) tags are included with EventKit.

More information on OSM tags can be found [here.](http://wiki.openstreetmap.org/wiki/Tags)

###Export Formats

The available export formats are listed under **Jobs > Export formats**. Each is identified by a display name, a slug to identify it in the backend, and a short description.

Currently, the listed formats include GeoTIFF, SQLite, KML, ESRI Shapefile, and GeoPackage. Of these, only GeoPackage is supported right now for most exports, and GeoTIFF for WCS.

###Export Profiles

Export profiles, found under **Jobs > Export profiles**, contain parameters common to all exports run by users in a particular group.

**Name** - Name of export profile
**Group** - User group to which to apply this profile's parameters
**Max extent** - Maximum AOI area for an export that's run under this profile, in square kilometers

The `DefaultExportProfile` is assigned to the `DefaultExportExtentGroup`, which has a max extent of 2500000 km^2.

###Export Provider Types

Each Export Provider has a corresponding Export Provider Type, preprogrammed in the EventKit backend and listed under **Jobs > Export Provider Types**. Currently, the supported provider types include:

**WCS** - Web Coverage Service, OWS service for thematic or non-imagery raster data such as elevation  
**ArcGIS Feature** - For feature (vector) data from an ArcGIS Server  
**WFS** - Web Feature Service, OWS service for feature (vector) data in the form of points, lines, or polygons  
**ArcGIS Raster** - For raster data from an ArcGIS Server  
**WMTS** - Web Map Tile Service, OWS service for tiled raster imagery, commonly used for basemaps  
**WMS** - Web Map Service, OWS service for generic raster imagery  
**OSM** - Open Street Map, common vector features with data grouped by theme (e.g. roads, buildings)  
**OSM Generic** - Open Street Map with data grouped by type (point, line, or polygon)  

All of these provider types currently export to the GeoPackage (.gpkg) format, except WCS, which exports to GeoTIFF (.tif).

###Export Providers

An Export Provider corresponds to an external source of data that EventKit can incorporate into a DataPack. These can be viewed and modified by administrators under **Jobs > Export Providers**.

To add an export provider, click the "Add Export Provider +" button in the upper right of the Export Providers section.

**Service Name** (required) - The display name of the service to add  
**Slug** (required) - A short, unique name given to the service for internal use  
**Service URL** (required) - URL for the endpoint to add. Parameters for WMS/WCS/WFS services are not necessary.  
**Preview URL** - URL to display on the front end in the map  
**Copyright** - Copyright information related to this provider  
**Description** - Any general information about the service to be added  
**Service Layer** (required for OWS) - Layer, coverage, or feature to request from provider server  
**Service Type** (required) - The Export Provider Type that describes this provider (e.g. OSM, WMS; see below)
**Seed from level** - Starting zoom level for exports to seed from  
**Seed to level** - Zoom level for exports to seed to  
**Configuration** - Additional configuration for this provider  
**User** - User to which to assign this provider  
**License** - License to assign to exports from this provider  
**Zip** - If checked, provide exports in a zipfile  
**Display** - If checked, display as a provider option when creating new datapacks in the front end  

###Jobs

Each DataPack that a user submits creats a Job, and these can be viewed by administrators under **Jobs > Jobs**. The name, description, project, region, and other attributes specified by the submitter can be changed, and the AOI and related provider tasks are shown as well.


###Provider Tasks

For each layer in a Job, a Provider Task is created. These may be viewed or deleted by administrators under **Jobs > Provider Tasks**. Removing a provider task will also remove the associated export run.

###User Licenses

Licenses are viewable under **Jobs > User Licenses**. Each export provider may optionally be assigned a license. To use EventKit, users must agree to each license that has been added.

##Logging

Under the section **Logging to Provide File/Django Model Crud Audit Trail > Audit Events**, a list of all task-related actions taken by users and administrators is displayed.

##Export Runs

The Export Runs corresponding to each DataPack are visible under **Tasks > Export Runs**. Selecting one will give the following fields:

**Job** - The job associated with this export run  
**User** - The user to whom this export run belongs  
**Zipfile url** - URL to access zipfile, if one has been created
**Expiration** - Date and time when this DataPack will expire
**Notified** - Last date and time at which the owner was notified about impending expiration, if applicable
**Deleted** - Checkbox indicating whether this datapack was deleted through the client (does not remove export run or data)

There is also an option to completely remove this export run. It's recommended to use the **Deleted** checkbox instead of removing the Export Run entirely.

