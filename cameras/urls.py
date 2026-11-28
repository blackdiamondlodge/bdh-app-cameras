from django.urls import path
from . import views

urlpatterns = [
    path('', views.camera_feed_view, name='cameras'),
    path('ptz/goto/', views.ptz_goto, name='ptz_goto'),
    path('door/unlock/', views.door_unlock, name='door_unlock'),
    path('api/talk/prepare/', views.talk_prepare, name='camera_talk_prepare'),
    path('go2rtc/<str:name>', views.go2rtc_script, name='camera_go2rtc_script'),
    path('diag/rtc/', views.rtc_diag, name='camera_rtc_diag'),
    path('diag/events/', views.rtc_events, name='camera_rtc_events'),
    path('events/', views.events_json, name='camera_events'),
    path('events/<str:event_id>/thumb/', views.event_thumb, name='camera_event_thumb'),
    path('snapshot/<str:camera_id>/', views.snapshot, name='camera_snapshot'),
    path('clip/<str:camera_id>/', views.clip, name='camera_clip'),
    path('popup/<str:camera_id>/', views.popup, name='camera_popup'),
    path('settings/', views.main_settings, name='cameras_settings'),
    path('settings/popups/', views.popup_settings, name='cameras_popups'),
    path('settings/guests/', views.guests_settings, name='cameras_guests'),
    path('settings/popups/entities/', views.popup_entities, name='cameras_popup_entities'),
    path('settings/nvrs/', views.nvr_settings, name='cameras_nvrs'),
    path('settings/nvrs/save/', views.nvr_save, name='cameras_nvr_save'),
    path('settings/nvrs/remove/', views.nvr_remove, name='cameras_nvr_remove'),
    path('settings/nvrs/test/', views.nvr_test, name='cameras_nvr_test'),
    path('settings/nvrs/access-test/', views.nvr_access_test, name='cameras_nvr_access_test'),
]