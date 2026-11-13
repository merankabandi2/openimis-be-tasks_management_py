from django.test import TestCase

from core.services import BaseService
from core.test_helpers import create_test_interactive_user
from tasks_management.models import Task, TaskGroup
from tasks_management.services import TaskService, UpdateCheckerLogicServiceMixin, on_task_complete_service_handler


class RecordingService(BaseService, UpdateCheckerLogicServiceMixin):
    OBJECT_TYPE = Task
    outcome = None
    calls = []
    write_before_outcome = False

    def __init__(self, user):
        self.user = user

    def update(self, obj_data):
        RecordingService.calls.append(obj_data)
        if RecordingService.write_before_outcome:
            TaskGroup.objects.create(
                code='written_by_service', completion_policy=TaskGroup.TaskGroupCompletionPolicy.ANY)
        if isinstance(RecordingService.outcome, Exception):
            raise RecordingService.outcome
        return RecordingService.outcome


class OnTaskCompleteServiceHandlerTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = create_test_interactive_user(username="test_handler_admin")
        cls.task_service = TaskService(cls.user)
        cls.handler = staticmethod(on_task_complete_service_handler(RecordingService))

    def setUp(self):
        RecordingService.calls = []
        RecordingService.write_before_outcome = False

    def _completed_task_result(self):
        created = self.task_service.create({
            'source': 'test_source',
            'entity': None,
            'status': Task.Status.RECEIVED,
            'executor_action_event': 'test_executor_action',
            'business_status': {},
            'business_event': 'RecordingService.update',
            'data': {'incoming_data': {'name': 'new'}},
        })
        self.assertTrue(created['success'])
        return self.task_service.complete_task({'id': created['data']['uuid']})

    def _run(self, outcome):
        RecordingService.outcome = outcome
        result = self._completed_task_result()
        errors = self.handler(result=result)
        task = Task.objects.get(id=result['data']['task']['id'])
        return errors, task

    def test_successful_service_leaves_task_completed(self):
        errors, task = self._run({'success': True, 'message': 'Ok', 'detail': '', 'data': {}})

        self.assertIsNone(errors)
        self.assertEqual(len(RecordingService.calls), 1)
        self.assertEqual(task.status, Task.Status.COMPLETED)

    def test_refused_service_marks_task_failed_with_reason(self):
        errors, task = self._run(
            {'success': False, 'message': 'Failed to update Plan', 'detail': 'type change refused', 'data': ''})

        self.assertEqual(len(RecordingService.calls), 1)
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertEqual(task.json_ext['failure']['message'], 'Failed to update Plan')
        self.assertEqual(task.json_ext['failure']['detail'], 'type change refused')
        self.assertEqual(errors, ['Failed to update Plan: type change refused'])

    def test_raising_service_marks_task_failed_with_reason(self):
        errors, task = self._run(ValueError('boom'))

        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertEqual(task.json_ext['failure']['detail'], 'boom')
        self.assertEqual(errors, ['boom'])

    def test_failure_rolls_back_what_the_service_wrote(self):
        RecordingService.write_before_outcome = True
        errors, task = self._run({'success': False, 'message': 'Failed', 'detail': 'refused', 'data': ''})

        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertFalse(TaskGroup.objects.filter(code='written_by_service').exists())
