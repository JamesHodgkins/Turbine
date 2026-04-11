
import json
from collections import deque

def calculate_execution_time(file_path, workers=2):
    with open(file_path, 'r') as f:
        tasks = json.load(f)

    # Build dependency graph and in-degree count
    graph = {t['id']: [] for t in tasks}
    in_degree = {t['id']: 0 for t in tasks}
    duration = {t['id']: t['duration'] for t in tasks}

    for task in tasks:
        for dep in task.get('dependencies', []):
            graph[dep].append(task['id'])
            in_degree[task['id']] += 1

    # Initialize queue with tasks that have no dependencies
    queue = deque([task_id for task_id in in_degree if in_degree[task_id] == 0])
    worker_queue = []
    current_time = 0
    completed_tasks = 0
    total_tasks = len(tasks)

    while completed_tasks < total_tasks:
        # Assign tasks to available workers
        while queue and len(worker_queue) < workers:
            task_id = queue.popleft()
            worker_queue.append({
                'task_id': task_id,
                'start_time': current_time,
                'end_time': current_time + duration[task_id]
            })

        if not worker_queue:
            break  # No tasks can be processed (shouldn't happen with valid input)

        # Find the task that will finish earliest
        earliest_task = min(worker_queue, key=lambda x: x['end_time'])
        current_time = earliest_task['end_time']
        worker_queue.remove(earliest_task)
        completed_tasks += 1

        # Update dependencies for tasks that depend on the completed task
        for dependent in graph[earliest_task['task_id']]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    return current_time

if __name__ == "__main__":
    print(f"Total Time: {calculate_execution_time('tasks.json', workers=2)}")
