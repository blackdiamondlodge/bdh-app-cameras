from django.db import models


class ProtectEvent(models.Model):
    """A UniFi Protect detection kept for the admin "last 24 hours" view."""

    event_id = models.CharField(max_length=64, unique=True)
    site = models.CharField(max_length=100)
    host = models.CharField(max_length=255)
    camera_id = models.CharField(max_length=64, db_index=True)
    camera_name = models.CharField(max_length=100, blank=True)
    kinds = models.JSONField(default=list)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(null=True, blank=True)
    score = models.IntegerField(default=0)
    thumbnail = models.BinaryField(null=True, blank=True)

    class Meta:
        ordering = ["-start"]

    def __str__(self):
        return f"{self.camera_name} {'/'.join(self.kinds)} {self.start:%Y-%m-%d %H:%M}"