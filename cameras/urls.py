from django.urls import path
from . import views

urlpatterns = [
    path('', views.camera_feed_view, name='cameras'),
    path('ptz/goto/', views.ptz_goto, name='ptz_goto'),
]