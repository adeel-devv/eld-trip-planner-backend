from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health),
    path("places/", views.places),
    path("reverse/", views.reverse),
    path("trips/plan/", views.plan_trip),
]
