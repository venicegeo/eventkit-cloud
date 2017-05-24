# -*- coding: utf-8 -*-
# Generated by Django 1.10.6 on 2017-05-22 16:33
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0016_exportprovider_zip'),
    ]

    operations = [
        migrations.AlterField(
            model_name='exportprovider',
            name='service_description',
            field=models.TextField(blank=True, default='', help_text='This information is used to provide information about the service.', null=True, verbose_name='Description'),
        ),
    ]
