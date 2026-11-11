from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ProtectEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(max_length=64, unique=True)),
                ("site", models.CharField(max_length=100)),
                ("host", models.CharField(max_length=255)),
                ("camera_id", models.CharField(db_index=True, max_length=64)),
                ("camera_name", models.CharField(blank=True, max_length=100)),
                ("kinds", models.JSONField(default=list)),
                ("start", models.DateTimeField(db_index=True)),
                ("end", models.DateTimeField(blank=True, null=True)),
                ("score", models.IntegerField(default=0)),
                ("thumbnail", models.BinaryField(blank=True, null=True)),
            ],
            options={"ordering": ["-start"]},
        ),
    ]