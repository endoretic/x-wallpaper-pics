plugins {
    id("com.android.application")
}

// 签名: CI 从仓库 Secret 解出 keystore, 通过环境变量传进来; 没有时只能打 debug 包
val keystorePath: String? = System.getenv("ANDROID_KEYSTORE_PATH")
// 用 CI 的运行序号当 versionCode, 保证每次打出的包都能覆盖安装旧版本
val buildNumber: Int = System.getenv("VERSION_CODE")?.toIntOrNull() ?: 1

android {
    namespace = "io.github.endoretic.wallpapersource"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.endoretic.wallpapersource"
        minSdk = 26
        targetSdk = 36
        versionCode = buildNumber
        versionName = "0.1.$buildNumber"
    }

    signingConfigs {
        if (keystorePath != null) {
            create("release") {
                storeFile = file(keystorePath)
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS") ?: "release"
                keyPassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release")
        }
    }
}

dependencies {
    implementation("com.google.android.apps.muzei:muzei-api:3.4.2")
    implementation("androidx.work:work-runtime:2.12.0")

    testImplementation("junit:junit:4.13.2")
    // Android 自带的 org.json 在 JVM 单元测试里只是空壳, 这里换成真实实现
    testImplementation("org.json:json:20260814")
}

tasks.withType<Test>().configureEach {
    testLogging {
        events("failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}
