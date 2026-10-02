import sys
sys.path.insert(0, ".")
import inspect
from routers.cohorts import get_cohort_health, intervene_cohort, CohortInterventionPayload

def test_endpoints():
    sig_health = inspect.signature(get_cohort_health)
    sig_intervene = inspect.signature(intervene_cohort)
    
    assert "cohort_id" in sig_health.parameters
    assert "cohort_id" in sig_intervene.parameters
    assert "payload" in sig_intervene.parameters
    
    p = CohortInterventionPayload(target_status="inactive", custom_message="Test nudge")
    assert p.target_status == "inactive"
    assert p.custom_message == "Test nudge"
    
    print("SUCCESS: Cohort health and intervention endpoints verified!")

if __name__ == "__main__":
    test_endpoints()
