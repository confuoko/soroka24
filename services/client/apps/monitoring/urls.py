from django.urls import path

from apps.monitoring import views

urlpatterns = [
    path("", views.case_list, name="case_list"),
    path("add/", views.case_add, name="case_add"),
    path("<int:pk>/", views.case_detail, name="case_detail"),
    path("<int:pk>/refresh/", views.case_refresh, name="case_refresh"),
    path("<int:pk>/delete/", views.case_delete, name="case_delete"),
]
