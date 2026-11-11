from django.urls import path
from . import views

urlpatterns = [
    path('', views.camera_feed_view, name='cameras'),
    path('ptz/goto/', views.ptz_goto, name='ptz_goto'),
    path('events/', views.events_json, name='camera_events'),
    path('events/<str:event_id>/thumb/', views.event_thumb, name='camera_event_thumb'),
    path('snapshot/<str:camera_id>/', views.snapshot, name='camera_snapshot'),
    path('clip/<str:camera_id>/', views.clip, name='camera_clip'),
    path('popup/<str:camera_id>/', views.popup, name='camera_popup'),
]