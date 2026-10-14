---
name: java-to-kotlin-android
description: "Converts Java Android code to Kotlin the way this codebase has already done it, using patterns mined from its past Java-to-Kotlin migration commits. Covers: constructors, null safety, properties and getters, collections, data classes, interfaces, lambdas and anonymous classes, operators, android views. Use when asked to migrate, convert or port a Java class, file or module to Kotlin in an Android MVVM project."
metadata:
  version: "2"
  generated-at: "2026-10-13"
  source-pairs: "17"
  source-commits: "17"
---

# Java to Kotlin migration rules (Android, MVVM)

Version 2, generated 2026-10-13 from 17 converted files across 17 migration commits in this repository's history.

## How to apply these rules

- Convert one file at a time and keep its behavior identical.
- Apply a rule when its Java construct appears in the file being converted.
- If a construct is not covered here, use idiomatic Kotlin and say so in the PR description.

## Rules

### Constructors

- **Constructors that only assign fields (this.x = x) become primary-constructor val/var parameters.**
  Seen in 9 of 17 converted files (medium confidence: established pattern). Accepted in 1 reviewed draft(s).

  ```java
  public Logger(String scope) {
      this.scope = scope;
  }
  ```
  ```kotlin
  class Logger(private val scope: String) {
  ```

### Null safety

- **Values that can be null in Java (null returns, null checks) are declared with nullable Kotlin types (T?).**
  Seen in 8 of 17 converted files (medium confidence: established pattern). Accepted in 1 reviewed draft(s).

  ```java
  return null;
  ```
  ```kotlin
  fun findById(id: String): User? = users.firstOrNull { it.id == id }
  ```

- **x != null ? x.foo() : default becomes x?.foo() ?: default.**
  Seen in 2 of 17 converted files (low confidence: seen only a few times; apply when the construct matches and mention it in the PR). Accepted in 1 reviewed draft(s).

  ```java
  displayName = user != null ? user.getName() : "Unknown";
  ```
  ```kotlin
  displayName = user?.name ?: "Unknown"
  ```

### Properties and getters

- **getFoo() calls become property access (.foo).**
  Seen in 8 of 17 converted files (medium confidence: established pattern). Accepted in 1 reviewed draft(s).

  ```java
  products.put(product.getId(), product);
  ```
  ```kotlin
  products[product.id] = product
  ```

- **A private field with a getter and no setter becomes a var with a private set.**
  Seen in 3 of 17 converted files (medium confidence: established pattern).

  ```java
  public String getLabel() {
      return label;
  }
  ```
  ```kotlin
  var label: String? = null
      private set
  ```

### Collections

- **new ArrayList<>() / HashMap<>() become mutableListOf() / mutableMapOf().**
  Seen in 4 of 17 converted files (medium confidence: established pattern). Accepted in 1 reviewed draft(s).

  ```java
  private final List<User> users = new ArrayList<>();
  ```
  ```kotlin
  private val users = mutableListOf<User>()
  ```

- **for-each loops that build or test collections become map/filter/any/all/count operations.**
  Seen in 4 of 17 converted files (medium confidence: established pattern). Accepted in 1 reviewed draft(s).

  ```java
  public double total() {
      double sum = 0;
      for (Product item : items) {
          sum += item.getPrice();
      }
      return sum;
  }
  ```
  ```kotlin
  fun total(): Double = items.sumOf { it.price }
  ```

- **A for-loop that returns the first match (or null) becomes firstOrNull { ... }.**
  Seen in 2 of 17 converted files (low confidence: seen only a few times; apply when the construct matches and mention it in the PR).

  ```java
  public User findById(String id) {
      for (User user : users) {
          if (user.getId().equals(id)) {
              return user;
          }
      }
      return null;
  }
  ```
  ```kotlin
  fun findById(id: String): User? = users.firstOrNull { it.id == id }
  ```

### Data classes

- **A Java class made of private fields and getters becomes a Kotlin data class.**
  Seen in 4 of 17 converted files (medium confidence: established pattern).

  ```java
  private final String id;
  // ...
  public String getId() {
      return id;
  }
  ```
  ```kotlin
  data class User(
      val id: String,
      val name: String,
      val email: String,
  )
  ```

### Interfaces

- **Single-method callback interfaces become fun interfaces so callers can pass lambdas.**
  Seen in 2 of 17 converted files (low confidence: seen only a few times; apply when the construct matches and mention it in the PR). Accepted in 1 reviewed draft(s).

  ```java
  public interface Callback {
      void onResult(User user);
  }
  ```
  ```kotlin
  fun interface Callback {
      fun onResult(user: User?)
  }
  ```

### Lambdas and anonymous classes

- **Anonymous single-method classes passed as callbacks become lambdas.**
  Seen in 2 of 17 converted files (low confidence: seen only a few times; apply when the construct matches and mention it in the PR). Accepted in 1 reviewed draft(s).

  ```java
  refresh.setOnClickListener(new View.OnClickListener() {
      @Override
      public void onClick(View v) {
          title.setText("Refreshed");
      }
  });
  ```
  ```kotlin
  binding.refresh.setOnClickListener {
      binding.title.text = "Refreshed"
  }
  ```

- **Anonymous classes that cannot be a lambda become object expressions (object : Type { ... }).**
  Seen in 1 of 17 converted files (low confidence: seen only a few times; apply when the construct matches and mention it in the PR). Accepted in 1 reviewed draft(s).

  ```java
  public void fetchDefault() {
      fetch("/ping", new Listener() {
          @Override
          public void onSuccess(String body) {
              System.out.println(body);
          }
          @Override
          public void onError(int code) {
              System.out.println("error " + code);
          }
      });
  }
  ```
  ```kotlin
  fun fetchDefault() {
      fetch("/ping", object : Listener {
          override fun onSuccess(body: String) {
              println(body)
          }
          override fun onError(code: Int) {
              println("error $code")
          }
      })
  }
  ```

### Operators

- **a.equals(b) becomes a == b.**
  Seen in 2 of 17 converted files (low confidence: seen only a few times; apply when the construct matches and mention it in the PR). Accepted in 1 reviewed draft(s).

  ```java
  if (user.getId().equals(id)) {
  ```
  ```kotlin
  fun findById(id: String): User? = users.firstOrNull { it.id == id }
  ```

### Android views

- **findViewById calls are replaced with view binding.**
  Seen in 1 of 17 converted files (low confidence: seen only a few times; apply when the construct matches and mention it in the PR).

  ```java
  title = findViewById(R.id.title);
  ```
  ```kotlin
  binding = ActivityMainBinding.inflate(layoutInflater)
  ```


## Unresolved: needs human review

These conflict with another source or were rejected in review. Do not apply them until a person resolves them.

- static members move into a companion object (or top-level declarations). (needs review: rejected in 1 reviewed draft(s), accepted in 0: utility classes should be objects)

## Not covered by this version

- Library usage conventions and coding standards are not included. This version is generated from commit history only.
