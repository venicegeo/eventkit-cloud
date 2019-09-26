# Generated by Django 2.2.3 on 2019-09-17 14:29

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', 'add_projections'),
    ]

    operations = [
        migrations.AddField(
            model_name='exportformat',
            name='supported_projections',
            field=models.ManyToManyField(
                related_name='supported_projections', to='jobs.Projection'),
        ),
    ]
