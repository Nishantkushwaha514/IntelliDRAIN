from django.urls import path
from .views import (
    BlockageDetectionView,
    FloodRiskAssessmentView,
    DrainListView,
    DrainDetailView,
    DrainHistoryView,
)

urlpatterns = [
    path('detect/', BlockageDetectionView.as_view(), name='blockage-detection'),
    path('flood-risk/', FloodRiskAssessmentView.as_view(), name='flood_risk_assessment'),
    path('drains/', DrainListView.as_view(), name='drain-list'),
    path('drains/<int:id>/', DrainDetailView.as_view(), name='drain-detail'),
    path('drains/<int:id>/history/', DrainHistoryView.as_view(), name='drain-history'),
]
