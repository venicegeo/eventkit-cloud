# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2016-10-31 05:08
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0005_job_zipfile_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='job',
            name='include_zipfile',
            field=models.BooleanField(default=False),
        ),
    ]
