# -*- coding: utf-8 -*-
# Generated by Django 1.10.6 on 2017-07-18 17:43
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0018_auto_20170623_2302'),
    ]

    operations = [
        migrations.AlterField(
            model_name='exporttask',
            name='result',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='export_task', to='tasks.FileProducingTaskResult'),
        ),
        migrations.AlterField(
            model_name='finalizerunhooktaskrecord',
            name='result',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='finalize_task', to='tasks.FileProducingTaskResult'),
        ),
    ]
